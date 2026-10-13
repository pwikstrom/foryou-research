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
  on to annotation. Drain the annotation queue with the batch annotator
  (``queue_annotator_batch``) to halve the cost.
* ``report`` — realised coverage of the selected sessions now.

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
            line.strip() for line in Path(args.exclude_file).read_text().splitlines() if line.strip()
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
    selected, summary = sc.select_sessions(
        table,
        target=args.target,
        month_cap=args.month_cap,
        budget=args.budget,
        per_participant_budget=args.per_participant_budget,
        min_plays=args.min_plays,
        coverage=args.coverage,
        feasible=args.feasible,
        salt=args.salt,
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
            "coverage": args.coverage,
            "feasible": args.feasible,
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


def cmd_queue(args) -> int:
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
    if args.dry_run:
        return 0
    for platform, items in by_platform.items():
        scrape_queues.append_to_scrape_queue(platform, items)
        print(f"Queued {len(items)} for scraping on {platform}")
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
    cov = manifest["params"]["coverage"]
    done = table["coverage"] >= cov
    print(
        f"{int(done.sum())} of {len(table)} selected sessions now at >= {cov:.0%} coverage; "
        f"median coverage {table['coverage'].median():.3f}"
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
    p.add_argument("--coverage", type=float, default=0.9)
    p.add_argument("--feasible", type=float, default=0.92)
    p.add_argument("--salt", required=True)
    p.add_argument("--exclude-file", default=None, help="collection ids to leave out, one per line")
    p.add_argument("--out", default=None, help="local folder for a copy (keep it gitignored)")
    p.add_argument("--dry-run", action="store_true")
    q = sub.add_parser("queue")
    q.add_argument("--name", required=True)
    q.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--name", required=True)
    args = parser.parse_args(argv)

    is_gcs, where = _storage_mode()
    print(f"Storage: {where}")
    if args.cmd in ("plan", "queue") and not getattr(args, "dry_run", False) and not is_gcs:
        print("REFUSING to write: storage is not GCS (set FYP_FORCE_GCS=1).")
        return 2
    return {"plan": cmd_plan, "queue": cmd_queue, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
