#!/usr/bin/env python3
"""Queue the annotation low-hanging fruit: near-fully-scraped collection-days.

A "collection-day" is one ``(collection_id, local_date)`` cell of play activity.
A cell whose items are almost all scraped is cheap to complete: annotating its
handful of remaining scraped-but-unannotated videos turns a partial day into a
fully enriched one, which is what day-level analyses (timelines, sessions,
binges) actually need. A cell at 40% scrape coverage gains far less per
annotation call.

The script:

1. Reads the recoded activity corpus (``collections_recoded.parquet``) and the
   per-item enrichment flags (``enrichment_status.parquet``).
2. Computes per-cell scrape coverage over unique play items.
3. Keeps cells at or above ``--min-coverage`` (and at least ``--min-items``
   items, so a 3-item day cannot qualify on a technicality).
4. Selects the items in those cells that are annotation-eligible under exactly
   the production rule (``scraped_ok`` AND ``video_downloaded`` AND NOT
   ``annotated_ok`` AND NOT ``annotated_fail`` AND duration under
   ``[machine] max_duration_for_annotation``).
5. Reports what it found and — only with ``--apply`` — appends the ids to the
   annotation queue ``to_annotate.json`` with the same atomic update the web
   UI uses. The worker is NOT started; run it from the web UI as usual.

Read-only by default. Nothing outside ``to_annotate.json`` is ever written.

Usage:
    source .venv/bin/activate

    # Dry run against the production GCS store (read-only):
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> \
        python scripts/adhoc/queue_high_coverage_days.py --min-coverage 0.90

    # Same, but actually append to the annotation queue:
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> \
        python scripts/adhoc/queue_high_coverage_days.py --min-coverage 0.90 --apply

Useful flags:
    --count-failed-as-covered  Treat permanently-failed scrapes as resolved when
                               computing coverage (a day that is 88% scraped and
                               12% unscrapeable is done, not incomplete).
    --platform tiktok          Restrict to one or more platforms.
    --limit 5000               Cap how many ids are queued. Days are queued
                               cheapest-first (fewest remaining annotations),
                               so a capped run completes as many days as the
                               budget allows; only the day straddling the cut
                               can be split. The printed cost curve shows the
                               --limit needed for any number of finished days.
    --report path.csv          Write the qualifying-cell table to a local CSV.
"""

import argparse
import ast
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[2]))

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf

ACTIVITY_FILE = "collections_recoded.parquet"
STATUS_FILE = "enrichment_status.parquet"
SCRAPES_FILE = "scrapes_recoded.parquet"
QUEUE_FILE = "to_annotate.json"

# The activity types that count as a video impression. Mirrors
# `organize_datasets._extract_selected_cells`, which defines the
# (collection_id, local_date) cells the rest of the Hub treats as a
# collection-day.
PLAY_TYPES = ("play", "observe")

BOOL_FLAGS = ("scraped_ok", "video_downloaded", "annotated_ok", "annotated_fail", "scrape_fail")






def _as_bool(series: pd.Series) -> pd.Series:
    """Coerce a nullable PyArrow boolean column to a plain NA-free bool Series.

    Uses ``where`` rather than ``fillna`` because a left join against the
    enrichment flags produces object-dtype columns, on which ``fillna``'s
    silent downcast is deprecated.
    """
    return series.where(series.notna(), False).astype(bool)






def load_cells(platforms: list[str] | None, verbose: bool) -> pd.DataFrame:
    """Load the unique ``(collection_id, local_date, item_id)`` play cells.

    Args:
        platforms: Optional allow-list of ``source_platform`` values.
        verbose: Print load timing from data_io.

    Returns:
        A DataFrame with one row per unique item within a collection-day.
    """
    columns = ["item_id", "collection_id", "local_date", "activity_type", "source_platform"]
    df = data_io.load_parquet_selective(
        storage_location="recoded",
        filename=ACTIVITY_FILE,
        columns=columns,
        verbose=verbose,
    )
    if df is None or df.empty:
        raise RuntimeError(f"'{ACTIVITY_FILE}' is missing or empty — nothing to analyse.")

    df = df[df["activity_type"].isin(PLAY_TYPES)]
    if platforms and "source_platform" in df.columns:
        df = df[df["source_platform"].isin(platforms)]

    df = df[["item_id", "collection_id", "local_date", "source_platform"]].dropna(
        subset=["item_id", "collection_id", "local_date"]
    )
    df = df.assign(
        item_id=df["item_id"].astype(str),
        collection_id=df["collection_id"].astype(str),
        local_date=pd.to_datetime(df["local_date"]).dt.strftime("%Y-%m-%d"),
    )
    return df.drop_duplicates(subset=["collection_id", "local_date", "item_id"])






def load_status(verbose: bool) -> pd.DataFrame:
    """Load the per-item enrichment flags keyed on ``item_id``."""
    columns = ["item_id", *BOOL_FLAGS]
    df = data_io.load_parquet_selective(
        storage_location="recoded",
        filename=STATUS_FILE,
        columns=columns,
        verbose=verbose,
    )
    if df is None or df.empty:
        raise RuntimeError(f"'{STATUS_FILE}' is missing or empty — enrichment state is unknown.")

    if "item_id" not in df.columns:
        df = df.reset_index().rename(columns={"index": "item_id"})

    df = df.assign(item_id=df["item_id"].astype(str))
    for flag in BOOL_FLAGS:
        df[flag] = _as_bool(df[flag]) if flag in df.columns else False
    return df[["item_id", *BOOL_FLAGS]].drop_duplicates(subset=["item_id"])






def load_durations(verbose: bool) -> pd.DataFrame:
    """Load ``item_id -> duration`` from the recoded scrape data.

    Returns:
        A one-row-per-item DataFrame, or an empty frame when the scrape file
        carries no usable duration column (the caller then skips the duration
        gate, exactly as the production queue builder does).
    """
    df = data_io.load_parquet_selective(
        storage_location="recoded",
        filename=SCRAPES_FILE,
        columns=["item_id", "duration"],
        verbose=verbose,
    )
    if df is None or df.empty or "duration" not in df.columns:
        return pd.DataFrame(columns=["item_id", "duration"])

    df = df.assign(item_id=df["item_id"].astype(str))
    # An item_id is unique per platform; sorting NA-last keeps a real duration
    # over a null one when the same id appears twice.
    df = df.sort_values("duration", na_position="last").drop_duplicates(subset=["item_id"])
    return df[["item_id", "duration"]]






def load_failed_ids() -> set[str]:
    """The item ids recorded as failed scrapes.

    Read from the failed-scrapes record rather than ``enrichment_status``,
    whose ``scrape_fail`` column is only as fresh as the last consolidation.

    Tolerates the mangled entries currently on the production store, where a
    whole record was stringified into the id position
    (``"{'item_id': '123', 'category': None}"``) — the id is recovered from the
    repr rather than counted as an unknown item.

    Returns:
        The failed item ids, or an empty set when the record is unreadable.
    """
    try:
        from fyp.scrape import load_failed_scrapes

        raw = load_failed_scrapes()
    except Exception as exc:
        print(f"  WARNING: could not read the failed-scrapes record ({exc}); "
              f"falling back to the enrichment_status flag")
        return set()

    ids: set[str] = set()
    mangled = 0
    for entry in raw:
        text = str(entry)
        if text.startswith("{") and "item_id" in text:
            try:
                item_id = ast.literal_eval(text).get("item_id")
            except (ValueError, SyntaxError):
                continue
            if item_id is not None:
                ids.add(str(item_id))
                mangled += 1
        else:
            ids.add(text)
    if mangled:
        print(f"  NOTE: {mangled:,} failed-scrape records were stored with the whole "
              f"record stringified into the id — ids recovered from the repr")
    return ids






def summarize_cells(cells: pd.DataFrame, count_failed_as_covered: bool) -> pd.DataFrame:
    """Aggregate per-item flags into per-collection-day coverage statistics.

    Args:
        cells: Item-level rows joined with the enrichment flags.
        count_failed_as_covered: Count permanently-failed scrapes towards
            coverage — a day is "done" when every item has been resolved one
            way or the other, not only when every item succeeded.

    Returns:
        One row per ``(collection_id, local_date)`` with item counts, coverage,
        and how many eligible-but-unannotated items the cell still holds.
    """
    # An item can be both scraped and present in the failed record (a transient
    # failure that a later attempt fixed), so "resolved" is a union, never a sum.
    cells = cells.assign(resolved=cells["scraped_ok"] | cells["scrape_fail"])

    grouped = cells.groupby(["collection_id", "local_date"], sort=False)
    stats = grouped.agg(
        n_items=("item_id", "size"),
        n_scraped=("scraped_ok", "sum"),
        n_scrape_failed=("scrape_fail", "sum"),
        n_resolved=("resolved", "sum"),
        n_annotated=("annotated_ok", "sum"),
        n_eligible=("eligible", "sum"),
    ).reset_index()

    covered = stats["n_resolved"] if count_failed_as_covered else stats["n_scraped"]
    stats["coverage"] = (covered / stats["n_items"]).round(4)
    stats["annot_coverage"] = (stats["n_annotated"] / stats["n_items"]).round(4)
    return stats.sort_values(["coverage", "n_eligible"], ascending=[False, False])






def print_cost_curve(candidates: pd.DataFrame, rows: int) -> None:
    """Print what each spend level buys, in the order items will be queued.

    Candidates arrive sorted cheapest-day-first, so a prefix of the list is
    always a whole number of finished collection-days. Reading down the
    ``items`` column gives the ``--limit`` needed to complete the matching
    number of days.

    Args:
        candidates: The final, ordered, deduplicated candidate rows.
        rows: How many day-size steps to print.
    """
    if candidates.empty:
        return

    per_day = candidates.groupby(["collection_id", "local_date"], sort=False).agg(
        block=("item_id", "size"),
        n_eligible=("n_eligible", "first"),
    )
    curve = per_day.groupby("n_eligible", sort=True).agg(
        days=("block", "size"),
        items=("block", "sum"),
    )
    curve["cum_days"] = curve["days"].cumsum()
    curve["cum_items"] = curve["items"].cumsum()

    print("\nCheapest days first — what each --limit buys:")
    print(f"  {'to annotate/day':>15} {'days':>8} {'days done':>12} {'--limit needed':>16}")
    for n_eligible, row in curve.head(rows).iterrows():
        print(f"  {int(n_eligible):>15,} {int(row['days']):>8,} "
              f"{int(row['cum_days']):>12,} {int(row['cum_items']):>16,}")
    if len(curve) > rows:
        print(f"  ... {len(curve) - rows:,} larger day-sizes not shown "
              f"({int(curve['cum_days'].iloc[-1]):,} days / "
              f"{int(curve['cum_items'].iloc[-1]):,} items in total)")






def cost_estimate(n_items: int) -> dict | None:
    """Rough USD spend for annotating ``n_items`` with the active backend.

    Mirrors ``web_interface.routes.management.enrichment._annotation_cost_estimate``
    but reads the backend selection through the ``fyp``-side settings module so
    the script needs no Flask import.

    Returns:
        A dict with the backend and estimate, or None when the active backend
        declares no pricing (local backends cost nothing per token).
    """
    try:
        from fyp.annotation.backends import variants
        from fyp.annotation.backends.settings import get_annotation_backend

        selection = get_annotation_backend()
        pricing = variants.selection_pricing(selection)
    except Exception:
        return None
    if not pricing:
        return None

    machine_cf = fyp_cf.get("machine", {})
    est_in = float(machine_cf.get("est_input_tokens_per_annotation", 15000))
    est_out = float(machine_cf.get("est_output_tokens_per_annotation", 1500))
    cost = n_items * (est_in * float(pricing.get("input", 0))
                      + est_out * float(pricing.get("output", 0))) / 1e6
    return {"backend": selection, "est_cost_usd": round(cost, 2)}






def append_to_queue(item_ids: list[str]) -> int:
    """Atomically append ids to ``to_annotate.json``.

    Uses the same compare-and-swap update as the web UI, so ids appended or
    pruned by a running annotation worker are never clobbered.

    Args:
        item_ids: The ids to add.

    Returns:
        The queue length after the update.
    """
    additions = {str(v) for v in item_ids}
    queue = data_io.update_json(
        storage_location="cache",
        filename=QUEUE_FILE,
        mutate=lambda current: sorted(
            {str(v) for v in (current if isinstance(current, list) else [])} | additions
        ),
        default=[],
    )
    return len(queue) if isinstance(queue, list) else 0






def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-coverage", type=float, default=0.90,
                        help="Minimum scrape coverage for a collection-day (default: 0.90).")
    parser.add_argument("--min-items", type=int, default=10,
                        help="Minimum unique play items in a collection-day (default: 10).")
    parser.add_argument("--count-failed-as-covered", action="store_true",
                        help="Count permanently-failed scrapes towards coverage.")
    parser.add_argument("--platform", action="append", default=None,
                        help="Restrict to a source_platform (repeatable).")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Also queue items whose previous annotation failed.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap how many ids to queue, cheapest days first.")
    parser.add_argument("--top", type=int, default=15,
                        help="How many day-size steps of the cost curve to print (default: 15).")
    parser.add_argument("--report", default=None,
                        help="Write the qualifying-cell table to this local CSV path.")
    parser.add_argument("--apply", action="store_true",
                        help="Append the selected ids to to_annotate.json (default: dry run).")
    parser.add_argument("--verbose", action="store_true", help="Verbose data_io load logging.")
    args = parser.parse_args()

    if not os.environ.get("FYP_FORCE_GCS") and not os.environ.get("K_SERVICE"):
        print("Refusing to run against local storage. This script targets the production "
              "GCS store — re-run with:\n"
              "  FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> python "
              f"{Path(__file__).name} ...")
        return 2

    max_duration = float(fyp_cf.get("machine", {}).get("max_duration_for_annotation", 300))

    print(f"Loading activity cells from '{ACTIVITY_FILE}' ...")
    cells = load_cells(args.platform, args.verbose)
    print(f"  {len(cells):,} unique (collection-day, item) rows")

    print(f"Loading enrichment flags from '{STATUS_FILE}' ...")
    status = load_status(args.verbose)
    print(f"  {len(status):,} items with enrichment state")

    cells = cells.merge(status, on="item_id", how="left")
    for flag in BOOL_FLAGS:
        cells[flag] = _as_bool(cells[flag])

    if args.count_failed_as_covered:
        # enrichment_status only carries scrape_fail as of the last
        # consolidation, so read the failed-scrape record directly — failures
        # banked since then would otherwise count against coverage.
        failed = load_failed_ids()
        if failed:
            cells["scrape_fail"] = cells["scrape_fail"] | cells["item_id"].isin(failed)
        n_failed = cells.loc[cells["scrape_fail"], "item_id"].nunique()
        print(f"  {n_failed:,} items in these collection-days are recorded as failed scrapes")

    print(f"Loading durations from '{SCRAPES_FILE}' ...")
    durations = load_durations(args.verbose)
    if durations.empty:
        print("  no duration column available — the duration gate is skipped")
        duration_ok = pd.Series(True, index=cells.index)
    else:
        cells = cells.merge(durations, on="item_id", how="left")
        # A missing duration passes, matching the production queue builder.
        duration_ok = (cells["duration"] < max_duration) | cells["duration"].isna()
        print(f"  {len(durations):,} items with a scraped duration "
              f"(cap: {max_duration:g}s)")

    # The production annotation-eligibility rule, applied per item.
    not_failed = True if args.retry_failed else ~cells["annotated_fail"]
    cells["eligible"] = (
        cells["scraped_ok"]
        & cells["video_downloaded"]
        & ~cells["annotated_ok"]
        & not_failed
        & duration_ok
    )

    stats = summarize_cells(cells, args.count_failed_as_covered)
    qualifying = stats[(stats["coverage"] >= args.min_coverage) & (stats["n_items"] >= args.min_items)]

    coverage_label = "coverage (scraped+failed)" if args.count_failed_as_covered else "coverage (scraped)"
    print()
    print(f"Collection-days total:                {len(stats):,}")
    print(f"Qualifying (>= {args.min_coverage:.0%} {coverage_label}, "
          f">= {args.min_items} items): {len(qualifying):,}")
    print(f"  ... of which still hold work:       "
          f"{int((qualifying['n_eligible'] > 0).sum()):,}")

    if qualifying.empty:
        print("\nNothing qualifies at this threshold. Nothing queued.")
        return 0

    work = qualifying[qualifying["n_eligible"] > 0]

    if args.report:
        report_path = Path(args.report).expanduser()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        qualifying.to_csv(report_path, index=False)
        print(f"\nWrote qualifying-cell report: {report_path}")

    # Item selection: eligible items inside qualifying cells. An item can sit in
    # several collection-days, so rank by the best cell it appears in and dedupe.
    #
    # Priority is cheapest-day-first: the fewer annotations a day still needs,
    # the sooner it is queued, so each spend closes as many collection-days as
    # possible. Scrape coverage only breaks ties between equally cheap days.
    # Items of one day share both keys, so a day is always a contiguous block
    # and only the day straddling a --limit cut can be split.
    cell_keys = pd.MultiIndex.from_frame(cells[["collection_id", "local_date"]])
    qualifying_keys = pd.MultiIndex.from_frame(qualifying[["collection_id", "local_date"]])
    in_qualifying = pd.Series(cell_keys.isin(qualifying_keys), index=cells.index)
    candidates = cells[in_qualifying & cells["eligible"]]
    candidates = candidates.merge(
        qualifying[["collection_id", "local_date", "coverage", "n_eligible"]],
        on=["collection_id", "local_date"], how="left",
    )
    candidates = candidates.sort_values(
        ["n_eligible", "coverage"], ascending=[True, False]
    ).drop_duplicates(subset=["item_id"])

    print(f"\nAnnotation-eligible items in qualifying days: {len(candidates):,}")

    already_queued: set[str] = set()
    if data_io.exists(storage_location="cache", filename=QUEUE_FILE):
        queue = data_io.load_json(storage_location="cache", filename=QUEUE_FILE)
        already_queued = {str(v) for v in queue} if isinstance(queue, list) else set()
    if already_queued:
        candidates = candidates[~candidates["item_id"].isin(already_queued)]
        print(f"Already in the queue ({len(already_queued):,} ids) — new: {len(candidates):,}")

    if args.top:
        print_cost_curve(candidates, args.top)

    if args.limit is not None and len(candidates) > args.limit:
        print(f"Capping at --limit {args.limit:,} (cheapest days first)")
        candidates = candidates.head(args.limit)

    selected = candidates["item_id"].astype(str).tolist()

    # How many qualifying days this batch finishes outright — the payoff that
    # makes near-complete days worth doing before deeper backlogs. An item can
    # belong to several days, so completion is measured against every eligible
    # item of a day being either in this batch or already queued.
    if selected:
        accounted = set(selected) | already_queued
        eligible_rows = cells[in_qualifying & cells["eligible"]]
        remaining = eligible_rows[~eligible_rows["item_id"].isin(accounted)]
        left_per_cell = remaining.groupby(["collection_id", "local_date"]).size()
        completed = len(qualifying) - int((left_per_cell > 0).sum())
        already_complete = len(qualifying) - len(work)
        print(f"Qualifying days finished by this batch: {completed - already_complete:,} "
              f"(leaving {len(qualifying) - completed:,} still incomplete)")

    cost = cost_estimate(len(selected))
    if cost:
        print(f"Estimated cost with backend '{cost['backend']}': ~${cost['est_cost_usd']:,.2f}")

    if not selected:
        print("\nNothing new to queue.")
        return 0

    if not args.apply:
        print(f"\nDRY RUN — would append {len(selected):,} ids to '{QUEUE_FILE}'. "
              f"Re-run with --apply to write.")
        return 0

    total = append_to_queue(selected)
    print(f"\nAppended {len(selected):,} ids. Queue now holds {total:,} items.")
    print("Start the annotation worker from the web UI when ready.")
    return 0




if __name__ == "__main__":
    raise SystemExit(main())
