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
  too; the new attempt records a reason either way. ``--reannotate`` also
  queues a random-mode manifest's videos annotated under another version, so
  that they are annotated again with the active one; the archive keeps both
  versions, and which one studies read follows the preferred version.
* ``report`` — realised coverage of the selected sessions now (``--rule``,
  ``--coverage``, ``--max-gap`` or ``--no-gap-rule`` override the manifest's
  rule).
* ``retry-annotations`` — queue for a new annotation attempt the failed
  annotations of long sessions that every video has been tried in and that
  would reach the annotated share (``--coverage``) if those annotations
  succeeded. Works on the study collections like ``plan`` (``--exclude-file``,
  or ``--collections-file`` to name them); ``--sample N`` queues a seeded
  random N for a trial. The annotation queue skips failed annotations
  otherwise.

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


def _read_ids(path: str | None) -> set[str]:
    """Ids listed one per line in ``path`` (empty when no path is given)."""
    if not path:
        return set()
    return {line.strip() for line in Path(path).read_text().splitlines() if line.strip()}


def _tiktok_plays(plays):
    """Plays of the collections whose plays are mostly TikTok, and those collections."""
    platform = plays.groupby("collection_id")["source_platform"].agg(
        lambda s: s.mode().iloc[0] if len(s.mode()) else ""
    )
    tiktok = set(platform[platform == "tiktok"].index.astype(str))
    return plays[plays["collection_id"].astype(str).isin(tiktok)], tiktok


def cmd_plan(args) -> int:
    import pandas as pd

    import fyp.core.data_io as data_io
    from fyp.core.runtime import code_version, utc_now_iso
    from web_interface.services import session_campaign as sc

    exclude = _read_ids(args.exclude_file)
    cids = _study_collections(exclude)
    plays, status, states = _load_inputs(cids)
    plays, tiktok = _tiktok_plays(plays)
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


def _for_you_plays(plays, collection_ids: list[str]):
    """For You plays per session: plays without an off-feed signal, flagged as
    the sessions build flags them (with the creators scraped so far)."""
    from fyp.analysis.sessions import inputs, publish

    events = inputs.load_search_follow(collection_ids)
    creators = inputs.load_creators(set(plays["item_id"].astype(str)))
    flagged = publish.attach_offfeed_signals(plays, creators, events, {})
    return flagged.loc[flagged["offfeed_signals"] == 0].groupby("session_id").size()


def _annotation_versions(item_ids):
    """Each annotated video's annotation version (item_id -> version)."""
    import pandas as pd

    import fyp.core.data_io as data_io

    df = data_io.load_parquet_selective(
        storage_location="recoded",
        filename="machine_annotations_recoded.parquet",
        columns=["item_id", "annotation_version"],
    )
    if df is None or df.empty:
        return pd.Series(dtype=object)
    df["item_id"] = df["item_id"].astype(str)
    df = df[df["item_id"].isin(set(map(str, item_ids)))].drop_duplicates("item_id", keep="last")
    return df.set_index("item_id")["annotation_version"]


def _parse_sizes(sizes: list[str]) -> list[dict[str, int]]:
    """``["5:8", "10:15"]`` -> ``[{"long": 5, "mid": 8}, {"long": 10, "mid": 15}]``."""
    out = []
    for s in sizes:
        long_n, mid_n = (int(x) for x in s.split(":"))
        out.append({"long": long_n, "mid": mid_n})
    return out


def _at_least(probabilities, k: int) -> float:
    """P(at least ``k`` successes) of independent trials (Poisson binomial)."""
    dist = [1.0]
    for p in probabilities:
        nxt = [0.0] * (len(dist) + 1)
        for j, q in enumerate(dist):
            nxt[j] += q * (1 - p)
            nxt[j + 1] += q * p
        dist = nxt
    return float(sum(dist[k:]))


def _random_summary(drawn, reach, to_scrape, to_annotate, retry, reannotate, cost) -> dict:
    """Aggregate counts of one random draw (no ids)."""
    out = {
        "sessions": len(drawn),
        "participants": int(drawn["collection_id"].nunique()),
        "by_band": {},
        "videos_to_scrape": len(to_scrape),
        "failures_without_a_reason_to_retry": len(retry),
        "videos_to_annotate_now": len(to_annotate),
        "expected_new_annotations": round(float(cost), 1),
        "videos_to_reannotate": len(reannotate),
    }
    for band, g in drawn.groupby("band"):
        r = reach.set_index("session_id").loc[g["session_id"]]
        short = (g["n_plays"] <= 80).to_numpy()
        out["by_band"][band] = {
            "sessions": len(g),
            "participants": int(g["collection_id"].nunique()),
            "meeting_the_rule_now": int(r["is_covered"].sum()),
            "expected_to_meet_the_rule": round(float(r["p_reach"].sum()), 1),
            "cannot_meet_it_videos_gone": int(r["impossible"].sum()),
            "by_quarter": g.groupby("quarter").size().sort_index().to_dict(),
            "plays": int(g["n_plays"].sum()),
            "plays_for_you": int(g["n_for_you"].sum()),
            "expected_for_you_plays_in_sessions_meeting_the_rule": round(
                float((r["p_reach"].to_numpy() * g["n_for_you"].to_numpy()).sum()), 1
            ),
            "sessions_up_to_80_plays": int(short.sum()),
            "expected_to_meet_the_rule_up_to_80_plays": round(
                float(r["p_reach"].to_numpy()[short].sum()), 1
            ),
        }
    # Participants expected to hold at least 3 sessions that meet the rule: in the long band (RQ1's
    # minimum) and among sessions of 12-80 plays of either band (RQ3's).
    p = reach.set_index("session_id")["p_reach"]
    d = drawn.assign(p=drawn["session_id"].map(p).to_numpy())
    long_p = d[d["band"] == "long"].groupby("collection_id")["p"].apply(list)
    rq3_p = d[(d["n_plays"] >= 12) & (d["n_plays"] <= 80)].groupby("collection_id")["p"].apply(list)
    out["expected_participants_with_3_long_meeting_the_rule"] = round(
        sum(_at_least(v, 3) for v in long_p), 1
    )
    out["expected_participants_with_3_sessions_of_12_to_80_meeting_the_rule"] = round(
        sum(_at_least(v, 3) for v in rq3_p), 1
    )
    out["expected_sessions_meeting_the_rule_per_participant_median"] = float(
        d.groupby("collection_id")["p"].sum().median()
    )
    return out


def cmd_plan_random(args) -> int:
    """``plan --mode random``: a stratified random draw within each participant."""
    import pandas as pd

    import fyp.core.data_io as data_io
    from fyp.core.runtime import code_version, utc_now_iso
    from fyp.scrape.failures import load_failed_scrapes_detail
    from web_interface.services import session_campaign as sc

    sizes = _parse_sizes(args.sizes)
    if not args.dry_run and len(sizes) != 1:
        raise SystemExit(
            "A manifest needs exactly one --sizes value; give several only with --dry-run."
        )
    only = _read_ids(args.collections_file)
    cids = sorted(only) if only else _study_collections(_read_ids(args.exclude_file))
    plays, status, states = _load_inputs(cids)
    plays, tiktok = _tiktok_plays(plays)
    yields = sc.enrichment_yields(status)
    yields["source"] = "measured over the collections' videos"
    if args.scrape_yield is not None or args.annotate_yield is not None:
        yields["scrape"] = args.scrape_yield if args.scrape_yield is not None else yields["scrape"]
        yields["annotate"] = (
            args.annotate_yield if args.annotate_yield is not None else yields["annotate"]
        )
        yields["source"] = "given (--scrape-yield / --annotate-yield)"
    table = sc.session_table(plays, states, yields["scrape"], yields["annotate"])
    table = sc.mark_rule(table, args.rule, args.coverage, args.coverage, args.max_gap)
    for_you = _for_you_plays(plays, sorted(tiktok))
    table["n_for_you"] = table["session_id"].map(for_you).fillna(0).astype(int)
    table["band"] = sc.session_bands(
        table, for_you, args.min_plays, args.min_for_you, (args.mid_min, args.min_plays - 1)
    )
    table["quarter"] = sc.quarter_of(table["start"]).to_numpy()
    ordered = sc.draw_order(table, args.salt)
    reach = sc.reach_probability(
        table, yields["scrape"], yields["annotate"], args.coverage, seed=args.seed
    )
    no_reason = {
        i for i, why in load_failed_scrapes_detail(consolidate=False).items() if why is None
    }
    versions = _annotation_versions(plays["item_id"].astype(str).unique())
    print(
        f"Collections: {len(tiktok)} TikTok collections; sessions: {len(table)}; "
        f"long {int((table['band'] == 'long').sum())}, 12-{args.min_plays - 1} plays "
        f"{int((table['band'] == 'mid').sum())}"
    )
    print(f"Yields: {yields}; salt {args.salt!r}")
    report = {
        "yields": yields,
        "salt": args.salt,
        "rule": args.rule,
        "coverage": args.coverage,
        "population": {
            "sessions": len(table),
            "by_band": table["band"].value_counts(dropna=False).to_dict(),
            "participants_by_band": table.groupby("band")["collection_id"].nunique().to_dict(),
            "by_band_and_quarter": {
                b: g.groupby("quarter").size().sort_index().to_dict()
                for b, g in table[table["band"].notna()].groupby("band")
            },
            "meeting_the_rule_now_by_band": table.groupby("band")["is_covered"].sum().to_dict(),
        },
        "sizes": {},
    }
    for size in sizes:
        drawn = sc.draw_stratified(ordered, size)
        to_scrape, to_annotate = sc.campaign_items(plays, states, drawn["session_id"])
        sel = set(drawn["session_id"])
        ids = set(plays.loc[plays["session_id"].astype(str).isin(sel), "item_id"].astype(str))
        retry = sorted(ids & no_reason)
        covered_ids = [i for i in ids if bool(states["covered"].get(i, False))]
        reannotate = sorted(i for i in covered_ids if versions.get(i) != args.current_version)
        cost = drawn["cost"].sum()
        summary = _random_summary(drawn, reach, to_scrape, to_annotate, retry, reannotate, cost)
        key = f"{size['long']}:{size['mid']}"
        report["sizes"][key] = summary
        print(f"Size {key}: {json.dumps(summary, default=str)}")
    if args.summary_json:
        Path(args.summary_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary_json).write_text(json.dumps(report, indent=1, default=str))
        print(f"Summary written to {args.summary_json} (aggregates only)")
    if args.dry_run:
        return 0
    size = sizes[0]
    manifest = {
        "name": args.name,
        "created_at": utc_now_iso(),
        "code_version": code_version(),
        "params": {
            "mode": "random",
            "per_participant": size,
            "min_plays": args.min_plays,
            "min_for_you": args.min_for_you,
            "mid_min": args.mid_min,
            "rule": args.rule,
            "coverage": args.coverage,
            "max_gap": args.max_gap,
            "salt": args.salt,
            "current_version": args.current_version,
            "collections": len(tiktok),
        },
        "yields": yields,
        "summary": report["sizes"][f"{size['long']}:{size['mid']}"],
        "sessions": drawn.assign(start=drawn["start"].astype(str))
        .drop(columns=["is_covered", "is_feasible"], errors="ignore")
        .to_dict("records"),
        "to_scrape": to_scrape,
        "to_annotate": to_annotate,
        "retry_no_reason": retry,
        "to_reannotate": reannotate,
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
    from fyp.scrape.failures import load_failed_scrapes_detail

    ids = _session_video_ids(manifest)
    detail = load_failed_scrapes_detail(consolidate=False)
    return sorted(i for i in ids if i in detail and detail[i] is None)


def _session_video_ids(manifest: dict) -> set[str]:
    """Every distinct video played in the manifest's selected sessions."""
    from fyp.analysis.sessions import inputs

    sessions = manifest.get("sessions") or []
    selected = {str(s["session_id"]) for s in sessions}
    cids = sorted({str(s["collection_id"]) for s in sessions})
    plays = inputs.load_plays(cids)
    return set(plays.loc[plays["session_id"].astype(str).isin(selected), "item_id"].astype(str))


def cmd_queue(args) -> int:
    import random

    import fyp.scrape.scrape_queues as scrape_queues
    from web_interface.services import collection_enrichment

    manifest = _load_manifest(args.name)
    if args.from_sessions:
        # Every video of the selected sessions, so videos that were not on the
        # manifest's lists (e.g. earlier failures that came back on a retry)
        # are queued too.
        ids = sorted(_session_video_ids(manifest))
        source = "videos in the selected sessions"
    else:
        ids = sorted(set(manifest.get("to_scrape") or []) | set(manifest.get("to_annotate") or []))
        source = "videos"
    status = collection_enrichment.load_status(ids)
    by_platform, annotate = collection_enrichment.classify_for_enrichment(
        ids, status, default_platform="tiktok"
    )
    print(
        f"Manifest {args.name}: {len(ids)} {source}; now {sum(map(len, by_platform.values()))} "
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
            Path(args.retry_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.retry_out).write_text("\n".join(retry) + "\n")
            print(f"Retry sample written to {args.retry_out} (keep it gitignored)")
    reannotate: list[str] = []
    if args.reannotate:
        from web_interface.services import session_campaign as sc

        listed = manifest.get("to_reannotate") or []
        reannotate = sc.reannotation_eligible(listed, collection_enrichment.load_status(listed))
        print(
            f"Videos annotated under another version: {len(listed)}; "
            f"{len(reannotate)} still have their media and are re-annotated"
        )
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
    if reannotate:
        collection_enrichment.queue_for_annotation(reannotate)
        print(f"Queued {len(reannotate)} for re-annotation with the active version")
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


def cmd_retry_annotations(args) -> int:
    import random

    from web_interface.services import collection_enrichment
    from web_interface.services import session_campaign as sc

    cids = _study_collections(_read_ids(args.exclude_file))
    only = _read_ids(args.collections_file)
    if only:
        cids = [c for c in cids if c in only]
    plays, status, states = _load_inputs(cids)
    plays, tiktok = _tiktok_plays(plays)
    sessions, items = sc.annotation_retry_plan(
        plays, status, states, coverage=args.coverage, min_plays=args.min_plays
    )
    print(
        f"Collections: {len(tiktok)} TikTok study collections. {len(sessions)} sessions of "
        f"{args.min_plays}+ plays from {sessions['collection_id'].nunique()} collections would "
        f"reach {args.coverage:.0%} annotated if their {len(items)} failed annotations succeeded"
    )
    if args.sample:
        items = sorted(random.Random(args.seed).sample(items, min(args.sample, len(items))))
        print(f"Sample: {len(items)} of them (seed {args.seed})")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("\n".join(items) + "\n")
        print(f"Ids written to {args.out} (keep it gitignored)")
    if args.dry_run:
        return 0
    collection_enrichment.queue_for_annotation(items)
    print(f"Queued {len(items)} for annotation (drain with queue_annotator_batch)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--name", required=True)
    p.add_argument(
        "--mode",
        choices=["cheapest", "random"],
        default="cheapest",
        help="cheapest: complete the cheapest feasible long sessions (the default); "
        "random: a stratified random draw within each participant by length band and quarter",
    )
    p.add_argument("--target", type=int, default=20)
    p.add_argument("--month-cap", type=int, default=4)
    p.add_argument(
        "--budget", type=float, default=None, help="expected annotations (cheapest mode)"
    )
    p.add_argument(
        "--sizes",
        nargs="+",
        default=["5:8"],
        help="random mode: LONG:MID sessions per participant; several only with --dry-run",
    )
    p.add_argument(
        "--min-for-you", type=int, default=50, help="random mode: For You plays of a long session"
    )
    p.add_argument(
        "--mid-min", type=int, default=12, help="random mode: fewest plays of the shorter band"
    )
    p.add_argument(
        "--scrape-yield",
        type=float,
        default=None,
        help="random mode: expected share of scrapes that succeed with media (default: measured)",
    )
    p.add_argument(
        "--annotate-yield",
        type=float,
        default=None,
        help="random mode: expected share of annotations that succeed (default: measured)",
    )
    p.add_argument(
        "--current-version",
        default=None,
        help="random mode: annotation version whose videos need no re-annotation",
    )
    p.add_argument(
        "--seed", type=int, default=0, help="random mode: seed of the expected-coverage simulation"
    )
    p.add_argument(
        "--collections-file", default=None, help="only these collection ids, one per line"
    )
    p.add_argument(
        "--summary-json", default=None, help="random mode: write the aggregate summary here"
    )
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
        "--from-sessions",
        action="store_true",
        help="consider every video of the selected sessions, not only the manifest's lists",
    )
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
    q.add_argument(
        "--reannotate",
        action="store_true",
        help="also queue for annotation the manifest's videos annotated under another version "
        "(a random-mode manifest's to_reannotate list), if their media is still stored",
    )
    q.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--name", required=True)
    r.add_argument("--rule", choices=["share", "attempted"], default=None)
    r.add_argument("--coverage", type=float, default=None)
    r.add_argument("--max-gap", type=int, default=None)
    r.add_argument("--no-gap-rule", action="store_true", help="report without the gap condition")
    ra = sub.add_parser("retry-annotations")
    ra.add_argument("--coverage", type=float, default=0.82, help="annotated share to reach")
    ra.add_argument("--min-plays", type=int, default=60)
    ra.add_argument(
        "--exclude-file", default=None, help="collection ids to leave out, one per line"
    )
    ra.add_argument(
        "--collections-file", default=None, help="only these collection ids, one per line"
    )
    ra.add_argument("--sample", type=int, default=0, help="queue a seeded random N (0: all)")
    ra.add_argument("--seed", type=int, default=0)
    ra.add_argument("--out", default=None, help="write the queued ids here (gitignored)")
    ra.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    is_gcs, where = _storage_mode()
    print(f"Storage: {where}")
    writes = args.cmd in ("plan", "queue", "retry-annotations")
    if writes and not getattr(args, "dry_run", False) and not is_gcs:
        print("REFUSING to write: storage is not GCS (set FYP_FORCE_GCS=1).")
        return 2
    if args.cmd == "plan" and args.mode == "cheapest" and args.budget is None:
        parser.error("plan --mode cheapest needs --budget")
    return {
        "plan": cmd_plan_random if getattr(args, "mode", "") == "random" else cmd_plan,
        "queue": cmd_queue,
        "report": cmd_report,
        "retry-annotations": cmd_retry_annotations,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
