#!/usr/bin/env python3
"""Plan, queue and report a session campaign: bring long viewing sessions to full coverage.

See ``web_interface/services/session_campaign.py`` for the selection rule.
Three subcommands:

* ``plan``   — select sessions and write a manifest (``--dry-run`` prints the
  summary only). The manifest lists the selected sessions and their missing
  videos, and is saved to the ``cache`` location as
  ``session_campaign__<name>.json`` plus a local copy (``--out``).
* ``queue``  — classify the manifest's videos against the CURRENT enrichment
  status and queue them: unscraped ones to their platform's scrape queue,
  scraped ones with media to ``to_annotate.json``. Idempotent; run it again
  after the scrapes have been consolidated, so that newly scraped videos move
  on to annotation (before consolidation, videos scraped since the last one
  still look unscraped and would be queued for scraping again). Drain the
  annotation queue with the batch annotator (``queue_annotator_batch``) to
  halve the cost. ``--retry-no-reason N`` also re-queues for scraping a random
  N of the selected sessions' videos recorded as failed before failure reasons
  were stored (August 2026), when retryable failures were recorded as final
  too; the new attempt records a reason either way.
* ``report`` — realised coverage of the selected sessions now (``--rule``,
  ``--coverage``, ``--max-gap`` or ``--no-gap-rule`` override the manifest's
  rule).

The manifest names collections, sessions and videos: keep its local copy in a
gitignored folder (``tmp/``). Production data lives in GCS:

    source .venv/bin/activate
    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=<bucket> python scripts/session_campaign.py plan \\
        --name sd-2026-10 --target 20 --month-cap 4 --budget 29000 --salt sd-2026-10 \\
        --exclude-file tmp/excluded_collections.txt --out tmp/session_campaign --dry-run
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))


def _storage_mode() -> tuple[bool, str]:
    """Return ``(is_gcs, description)`` for the resolved data storage."""
    from fyp.core.fyp_config import fyp_cf

    use_gcs = bool(fyp_cf.get("data_io", {}).get("use_gcs_for_data"))
    if use_gcs:
        bucket = fyp_cf.get("data_io", {}).get("GCS_bucket_name") or "<unset>"
        return True, f"GCS bucket={bucket!r}"
    return False, f"LOCAL dir={fyp_cf.get('paths', {}).get('local_data')!r}"


def _manifest_filename(name: str) -> str:
    return f"session_campaign__{name}.json"


def _load_inputs(collection_ids: list[str]):
    """Play rows of the collections, their videos' states and the yields."""
    from fyp.analysis.sessions import inputs
    from web_interface.services import collection_enrichment, session_campaign

    plays = inputs.load_plays(collection_ids)
    status = collection_enrichment.load_status(plays["item_id"].astype(str).unique())
    states = session_campaign.item_states(plays["item_id"].astype(str).unique(), status)
    return plays, status, states


def _study_collections(exclude: set[str]) -> list[str]:
    """TikTok collections selected by at least one study, minus ``exclude``."""
    from fyp.analysis.sessions import inputs

    cids = sorted(inputs.compute_coverage_spec(clip=False))
    return [c for c in cids if c not in exclude]


def cmd_plan(args) -> int:
    import pandas as pd

    import fyp.core.data_io as data_io
    from fyp.core.runtime import code_version, utc_now_iso
    from web_interface.services import session_campaign as sc

    exclude = set()
    if args.exclude_file:
        exclude = {
            line.strip()
            for line in Path(args.exclude_file).read_text().splitlines()
            if line.strip()
        }
    cids = _study_collections(exclude)
    plays, status, states = _load_inputs(cids)
    platform = plays.groupby("collection_id")["source_platform"].agg(
        lambda s: s.mode().iloc[0] if len(s.mode()) else ""
    )
    tiktok = set(platform[platform == "tiktok"].index.astype(str))
    plays = plays[plays["collection_id"].astype(str).isin(tiktok)]
    yields = sc.enrichment_yields(status)
    table = sc.session_table(plays, states, yields["scrape"], yields["annotate"])
    table = sc.mark_rule(table, args.rule, args.coverage, args.feasible, args.max_gap)
    preselected: set[str] = set()
    for earlier in args.after or []:
        preselected |= {str(s["session_id"]) for s in _load_manifest(earlier).get("sessions") or []}
    selected, summary = sc.select_sessions(
        table,
        target=args.target,
        month_cap=args.month_cap,
        budget=args.budget,
        per_participant_budget=args.per_participant_budget,
        min_plays=args.min_plays,
        salt=args.salt,
        preselected=preselected,
    )
    to_scrape, to_annotate = sc.campaign_items(plays, states, selected["session_id"])
    print(f"Collections: {len(tiktok)} TikTok study collections ({len(exclude)} excluded)")
    print(f"Yields: {yields}")
    print(json.dumps(summary, indent=1, default=str))
    print(f"Videos: {len(to_scrape)} to scrape, {len(to_annotate)} to annotate now")
    if args.dry_run:
        return 0
    manifest = {
        "name": args.name,
        "created_at": utc_now_iso(),
        "code_version": code_version(),
        "params": {
            "target": args.target,
            "month_cap": args.month_cap,
            "budget": args.budget,
            "per_participant_budget": args.per_participant_budget,
            "min_plays": args.min_plays,
            "rule": args.rule,
            "coverage": args.coverage,
            "feasible": args.feasible,
            "max_gap": args.max_gap,
            "after": list(args.after or []),
            "salt": args.salt,
            "excluded_collections": len(exclude),
        },
        "yields": yields,
        "summary": summary,
        "sessions": selected.assign(start=selected["start"].astype(str))
        .drop(columns=["round"])
        .to_dict("records"),
        "to_scrape": to_scrape,
        "to_annotate": to_annotate,
    }
    manifest["hash"] = sc.manifest_hash(manifest)
    data_io.save_json(
        data=manifest, storage_location="cache", filename=_manifest_filename(args.name)
    )
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / _manifest_filename(args.name)).write_text(json.dumps(manifest, default=str))
        pd.DataFrame(manifest["sessions"]).to_csv(out / f"{args.name}_sessions.csv", index=False)
    print(f"Manifest saved (hash {manifest['hash'][:16]}…)")
    return 0


def _load_manifest(name: str) -> dict:
    import fyp.core.data_io as data_io

    manifest = data_io.load_json(storage_location="cache", filename=_manifest_filename(name))
    if not manifest:
        raise SystemExit(f"No manifest {_manifest_filename(name)!r} in the cache location.")
    return manifest


def _no_reason_failures(manifest: dict) -> list[str]:
    """Videos in the manifest's sessions recorded as failed with no reason.

    Records written before failure reasons were stored (August 2026) carry
    none, and the scraper then recorded every failure, retryable ones
    included, as final. Read without consolidating the record files.
    """
    from fyp.analysis.sessions import inputs
    from fyp.scrape.failures import load_failed_scrapes_detail

    sessions = manifest.get("sessions") or []
    selected = {str(s["session_id"]) for s in sessions}
    cids = sorted({str(s["collection_id"]) for s in sessions})
    plays = inputs.load_plays(cids)
    ids = set(plays.loc[plays["session_id"].astype(str).isin(selected), "item_id"].astype(str))
    detail = load_failed_scrapes_detail(consolidate=False)
    return sorted(i for i in ids if i in detail and detail[i] is None)


def cmd_queue(args) -> int:
    import random

    import fyp.scrape.scrape_queues as scrape_queues
    from web_interface.services import collection_enrichment

    manifest = _load_manifest(args.name)
    ids = sorted(set(manifest.get("to_scrape") or []) | set(manifest.get("to_annotate") or []))
    status = collection_enrichment.load_status(ids)
    by_platform, annotate = collection_enrichment.classify_for_enrichment(
        ids, status, default_platform="tiktok"
    )
    print(
        f"Manifest {args.name}: {len(ids)} videos; now {sum(map(len, by_platform.values()))} "
        f"to scrape, {len(annotate)} to annotate"
    )
    retry: list[str] = []
    if args.retry_no_reason:
        failed = _no_reason_failures(manifest)
        k = len(failed) if args.retry_no_reason < 0 else min(args.retry_no_reason, len(failed))
        retry = sorted(random.Random(args.seed).sample(failed, k))
        print(
            f"Failures with no recorded reason in the selected sessions: {len(failed)}; "
            f"retrying {len(retry)} (seed {args.seed})"
        )
        if args.retry_out:
            Path(args.retry_out).write_text("\n".join(retry) + "\n")
            print(f"Retry sample written to {args.retry_out} (keep it gitignored)")
    if args.dry_run:
        return 0
    if args.retry_only:
        # Only the earlier failures: safe before consolidation, when the
        # manifest's other videos may already be scraped but look unscraped.
        by_platform, annotate = {}, []
    for platform, items in by_platform.items():
        scrape_queues.append_to_scrape_queue(platform, items)
        print(f"Queued {len(items)} for scraping on {platform}")
    if retry:
        # The manifest's sessions are TikTok sessions (plan keeps TikTok only).
        scrape_queues.append_to_scrape_queue("tiktok", retry)
        print(f"Queued {len(retry)} earlier failures for a new scrape attempt on tiktok")
    if annotate:
        collection_enrichment.queue_for_annotation(annotate)
        print(f"Queued {len(annotate)} for annotation (drain with queue_annotator_batch)")
    return 0


def cmd_report(args) -> int:
    import pandas as pd

    from web_interface.services import session_campaign as sc

    manifest = _load_manifest(args.name)
    sessions = pd.DataFrame(manifest.get("sessions") or [])
    if sessions.empty:
        print("No sessions in the manifest.")
        return 0
    cids = sorted(sessions["collection_id"].astype(str).unique())
    plays, status, states = _load_inputs(cids)
    plays = plays[plays["session_id"].astype(str).isin(set(sessions["session_id"].astype(str)))]
    table = sc.session_table(plays, states)
    prm = manifest["params"]
    rule = args.rule or prm.get("rule", "share")
    cov = args.coverage if args.coverage is not None else prm["coverage"]
    max_gap = prm.get("max_gap")
    if args.no_gap_rule:
        max_gap = None
    elif args.max_gap is not None:
        max_gap = args.max_gap
    table = sc.mark_rule(table, rule, cov, prm.get("feasible", cov), max_gap)
    done = table["is_covered"]
    print(
        f"{int(done.sum())} of {len(table)} selected sessions now meet the rule "
        f"({rule}, {cov:.0%}, max gap {max_gap}); "
        f"median annotated share {table['coverage'].median():.3f}"
    )
    print(
        f"Still to scrape: {int(table['n_to_scrape'].sum())}, "
        f"to annotate: {int(table['n_to_annotate'].sum())}"
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--name", required=True)
    p.add_argument("--target", type=int, default=20)
    p.add_argument("--month-cap", type=int, default=4)
    p.add_argument("--budget", type=float, required=True, help="expected annotations")
    p.add_argument("--per-participant-budget", type=float, default=None)
    p.add_argument("--min-plays", type=int, default=60)
    p.add_argument(
        "--rule",
        choices=["share", "attempted"],
        default="share",
        help="share: >= coverage scraped and annotated; attempted: every video tried "
        "and >= coverage annotated",
    )
    p.add_argument("--coverage", type=float, default=0.9)
    p.add_argument("--feasible", type=float, default=0.92)
    p.add_argument(
        "--max-gap",
        type=int,
        default=None,
        help="also require no run of more than N consecutive plays without annotation",
    )
    p.add_argument(
        "--after",
        nargs="*",
        default=None,
        help="earlier campaign names whose sessions count as already selected",
    )
    p.add_argument("--salt", required=True)
    p.add_argument("--exclude-file", default=None, help="collection ids to leave out, one per line")
    p.add_argument("--out", default=None, help="local folder for a copy (keep it gitignored)")
    p.add_argument("--dry-run", action="store_true")
    q = sub.add_parser("queue")
    q.add_argument("--name", required=True)
    q.add_argument(
        "--retry-no-reason",
        type=int,
        default=0,
        help="also queue N videos of the selected sessions that were recorded as failed "
        "before failure reasons were stored, drawn at random (-1: all)",
    )
    q.add_argument("--seed", type=int, default=0, help="seed for the --retry-no-reason draw")
    q.add_argument("--retry-out", default=None, help="write the retried ids here (gitignored)")
    q.add_argument(
        "--retry-only",
        action="store_true",
        help="queue only the --retry-no-reason videos, not the manifest's (safe before consolidation)",
    )
    q.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--name", required=True)
    r.add_argument("--rule", choices=["share", "attempted"], default=None)
    r.add_argument("--coverage", type=float, default=None)
    r.add_argument("--max-gap", type=int, default=None)
    r.add_argument("--no-gap-rule", action="store_true", help="report without the gap condition")
    args = parser.parse_args(argv)

    is_gcs, where = _storage_mode()
    print(f"Storage: {where}")
    if args.cmd in ("plan", "queue") and not getattr(args, "dry_run", False) and not is_gcs:
        print("REFUSING to write: storage is not GCS (set FYP_FORCE_GCS=1).")
        return 2
    return {"plan": cmd_plan, "queue": cmd_queue, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
