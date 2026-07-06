#!/usr/bin/env python3
"""Ad-hoc test script for fyp.structure_sentinel (structure-drift detection).

Covers: typed key-path extraction, zip fingerprints, baseline learning,
structure/stat deviation scoring, the small-sample learning gate, and the
end-to-end load_raw quarantine interception + approve flow.

Usage:
    source .fypenv314/bin/activate
    python tests/test_structure_sentinel.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import pandas as pd

from fyp import structure_sentinel as ss


PASSED = 0
FAILED = 0




def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion outcome."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")




def make_ig_zip(path: str, rename_timestamp: bool = False, drop_likes: bool = False,
                extra_key: bool = False) -> None:
    """Write a synthetic Instagram DDP zip (label_values-era liked_posts +
    classic stories_viewed) with optional mutations."""
    ts_key = "WatchDate" if rename_timestamp else "timestamp"
    liked = {
        "likes_media_likes": [
            {
                "title": f"author_{i}",
                "string_list_data": [{
                    "href": f"https://www.instagram.com/reel/AbCdEf{i:03}/",
                    ts_key: 1700000000 + i * 60,
                }],
            }
            for i in range(30)
        ]
    }
    if extra_key:
        for record in liked["likes_media_likes"]:
            record["brand_new_field"] = "surprise"
    stories = [
        {
            "label_values": [
                {"label": "URL", "value": f"https://www.instagram.com/reel/XyZaBc{i:03}/"},
                {"label": "Caption", "value": f"caption {i}"},
            ],
            "timestamp": 1700100000 + i * 60,
        }
        for i in range(30)
    ]
    with zipfile.ZipFile(path, "w") as zf:
        if not drop_likes:
            zf.writestr("your_instagram_activity/likes/liked_posts.json", json.dumps(liked))
        zf.writestr(
            "your_instagram_activity/story_interactions/stories_viewed.json",
            json.dumps(stories),
        )




def test_key_paths() -> None:
    print("\n[key_paths_of]")
    obj = {"a": {"b": [{"c": 1, "d": "x"}]}, "e": None}
    paths = ss.key_paths_of(obj)
    check("nested typed paths", paths == {"a.b[].c|int", "a.b[].d|str", "e|null"}, str(paths))
    check("stable across calls", ss.key_paths_of(obj) == paths)
    check("base_path strips type", ss.base_path_of("a.b[].c|int") == "a.b[].c")
    dynamic = {"Chat History with donor123:": [{"m": 1}]}
    paths2 = ss.key_paths_of(dynamic)
    check("dynamic key collapsed", paths2 == {"chat history with *[].m|int"}, str(paths2))




def test_zip_fingerprint_and_structure_eval(tmp: str) -> None:
    print("\n[fingerprint_zip + evaluate_structure]")
    suffixes = [
        "story_interactions/stories_viewed.json",
        "likes/liked_posts.json",
    ]

    good = os.path.join(tmp, "good.zip")
    make_ig_zip(good)
    fp_good = ss.fingerprint_zip(good, suffixes)
    check("both members found", sorted(fp_good["member_paths"]) == sorted(suffixes),
          str(fp_good["member_paths"]))
    check("member-scoped key paths",
          any(p.startswith("likes/liked_posts.json::") for p in fp_good["key_paths"]))

    baseline = ss._empty_baseline()
    for i in range(5):
        fn = f"good_{i}.zip"
        make_ig_zip(os.path.join(tmp, fn))
        fp = ss.fingerprint_zip(os.path.join(tmp, fn), suffixes)
        ss.learn_file(baseline, fp, {"rows_per_mb": 800 + i * 10}, None, fn)
    check("baseline learned 5", baseline["n_accepted"] == 5)

    findings = ss.evaluate_structure(fp_good, baseline)
    check("good file: no structure findings", findings == [], str(findings))
    check("good file: status ok", ss.status_from_findings(findings, 5) == "ok")

    renamed = os.path.join(tmp, "renamed.zip")
    make_ig_zip(renamed, rename_timestamp=True)
    findings = ss.evaluate_structure(ss.fingerprint_zip(renamed, suffixes), baseline)
    codes = {f["code"] for f in findings}
    check("renamed key: missing_core_paths", "missing_core_paths" in codes, str(codes))
    check("renamed key: quarantined", ss.status_from_findings(findings, 5) == "quarantined")

    dropped = os.path.join(tmp, "dropped.zip")
    make_ig_zip(dropped, drop_likes=True)
    findings = ss.evaluate_structure(ss.fingerprint_zip(dropped, suffixes), baseline)
    codes = {f["code"] for f in findings}
    check("missing member detected", "missing_member" in codes, str(codes))
    check("missing member: no double-report of its paths",
          "missing_core_paths" not in codes, str(codes))
    check("missing member: quarantined", ss.status_from_findings(findings, 5) == "quarantined")

    additive = os.path.join(tmp, "additive.zip")
    make_ig_zip(additive, extra_key=True)
    findings = ss.evaluate_structure(ss.fingerprint_zip(additive, suffixes), baseline)
    codes = {f["code"] for f in findings}
    check("additive key: new_key_paths warn", codes == {"new_key_paths"}, str(codes))
    check("additive key: status warn", ss.status_from_findings(findings, 5) == "warn")

    check("small-sample gate: learning",
          ss.status_from_findings(
              ss.evaluate_structure(ss.fingerprint_zip(renamed, suffixes),
                                    _partial_baseline(tmp, suffixes, 2)), 2
          ) == "learning")




def _partial_baseline(tmp: str, suffixes: list, n: int) -> dict:
    """Baseline learned from only ``n`` good files (small-sample gate tests)."""
    baseline = ss._empty_baseline()
    for i in range(n):
        fn = f"partial_{i}.zip"
        make_ig_zip(os.path.join(tmp, fn))
        fp = ss.fingerprint_zip(os.path.join(tmp, fn), suffixes)
        ss.learn_file(baseline, fp, None, None, fn)
    return baseline




def test_stat_eval() -> None:
    print("\n[evaluate_stats]")
    baseline = ss._empty_baseline()
    for i in range(6):
        ss.learn_file(
            baseline, None,
            {"rows_per_mb": 800.0 + i * 5},
            {"kept_ratio": 0.95 + i * 0.005, "null_item_id_frac": 0.01,
             "activity_types": {"play": 100, "fave": 20}},
            f"stat_{i}",
        )

    findings = ss.evaluate_stats(
        {"kept_ratio": 0.96, "rows_per_mb": 810.0, "null_item_id_frac": 0.01},
        {"play": 90, "fave": 25}, baseline,
    )
    check("in-range stats: no findings", findings == [], str(findings))

    findings = ss.evaluate_stats(
        {"kept_ratio": 0.42, "rows_per_mb": 810.0, "null_item_id_frac": 0.01},
        {"play": 90, "fave": 25}, baseline,
    )
    hard = [f for f in findings if f["code"] == "stat_outlier_hard"]
    check("garbled timestamps: kept_ratio hard outlier", len(hard) == 1, str(findings))
    check("garbled timestamps: quarantined",
          ss.status_from_findings(findings, 6) == "quarantined")

    findings = ss.evaluate_stats(
        {"kept_ratio": 0.96}, {"fave": 25}, baseline,
    )
    codes = {f["code"] for f in findings}
    check("dominant type missing: quarantine", "dominant_type_missing" in codes, str(codes))

    findings = ss.evaluate_stats(
        {"kept_ratio": 0.96}, {"play": 90, "fave": 20, "comment": 4}, baseline,
    )
    codes = {f["code"] for f in findings}
    check("new activity type: warn only", codes == {"new_activity_type"}, str(codes))
    check("new activity type: status warn", ss.status_from_findings(findings, 6) == "warn")

    small = ss._empty_baseline()
    for i in range(3):
        ss.learn_file(small, None, None, {"kept_ratio": 0.95}, f"s_{i}")
    findings = ss.evaluate_stats({"kept_ratio": 0.10}, None, small)
    check("stat gate below MIN_ACCEPTED_FOR_STAT_CHECKS", findings == [], str(findings))

    check("learn_file idempotent",
          (lambda b: (ss.learn_file(b, None, {"m": 1.0}, None, "x"),
                      ss.learn_file(b, None, {"m": 1.0}, None, "x"),
                      b["n_accepted"])[-1])(ss._empty_baseline()) == 1)




def test_load_raw_interception() -> None:
    print("\n[load_raw interception + approve flow]")
    from fyp.fyp_config import fyp_cf
    from fyp.ingest import ForYouBaseCollection

    raw_dir = os.path.join(fyp_cf["paths"]["activity_data"], "testplat", "sentinel_test_raw")
    os.makedirs(raw_dir, exist_ok=True)

    class SentinelTestCollection(ForYouBaseCollection):
        source_platform = "testplat"
        raw_path = "sentinel_test_raw"

        def __init__(self, verbose: bool = False):
            super().__init__(verbose=verbose)
            self.data_source = "test"

        def load_single_raw(self, filename: str) -> pd.DataFrame:
            records = json.loads(Path(os.path.join(raw_dir, filename)).read_text())
            return pd.DataFrame.from_records(records)

        def process_single(self, df: pd.DataFrame) -> pd.DataFrame:
            return df

    def write_fixture(fn: str, key: str = "watched_at") -> None:
        records = [{"item_id": f"vid{i:04}", key: 1700000000 + i} for i in range(30)]
        Path(os.path.join(raw_dir, fn)).write_text(json.dumps(records))

    try:
        for i in range(3):
            write_fixture(f"donor_{i}.json")
        write_fixture("mutant.json", key="viewed_on")

        # In-memory persistence so the test never touches the real recoded files.
        ss.save_baselines = lambda state: None
        ss.save_verdicts = lambda state: None
        ss.load_verdicts = lambda: {"schema_version": 1, "files": {}}

        sentinel = ss.StructureSentinel.__new__(ss.StructureSentinel)
        sentinel.baselines = {"schema_version": 1, "baselines": {}}
        sentinel.observations = {}

        coll = SentinelTestCollection()
        baseline = sentinel._baseline_for(coll)
        for i in range(3):
            fn = f"donor_{i}.json"
            ss.learn_file(baseline, coll.fingerprint_raw(fn), None, None, fn)

        coll.sentinel = sentinel
        coll.load_raw(skip_these_raw_files=[f"donor_{i}.json" for i in range(3)])
        check("mutant quarantined", "mutant.json" in coll.quarantined_this_run,
              str(list(coll.quarantined_this_run)))
        loaded_files = set(coll.data["raw_file"].unique()) if len(coll.data) else set()
        check("mutant rows withheld", "mutant.json" not in loaded_files, str(loaded_files))

        verdict = coll.quarantined_this_run.get("mutant.json") or {}
        codes = {f["code"] for f in verdict.get("findings", [])}
        check("mutant verdict has missing_core_paths", "missing_core_paths" in codes, str(codes))

        # Approve simulation: fold the stored fingerprint into the baseline,
        # then a fresh run ingests the file.
        ss.learn_file(baseline, verdict.get("fingerprint"), verdict.get("raw_stats"),
                      None, "mutant.json", approved_by="tester")
        coll2 = SentinelTestCollection()
        coll2.sentinel = sentinel
        coll2.load_raw(skip_these_raw_files=[f"donor_{i}.json" for i in range(3)])
        loaded_files = set(coll2.data["raw_file"].unique()) if len(coll2.data) else set()
        check("approved mutant ingested on re-run", "mutant.json" in loaded_files,
              str(loaded_files))
        check("no quarantine after approval", coll2.quarantined_this_run == {},
              str(coll2.quarantined_this_run))

        # Sentinel disabled → behaves exactly as before.
        coll3 = SentinelTestCollection()
        coll3.load_raw(skip_these_raw_files=[f"donor_{i}.json" for i in range(3)])
        loaded_files = set(coll3.data["raw_file"].unique()) if len(coll3.data) else set()
        check("sentinel=None ingests everything", "mutant.json" in loaded_files,
              str(loaded_files))
    finally:
        shutil.rmtree(os.path.dirname(raw_dir), ignore_errors=True)




if __name__ == "__main__":
    tmp = tempfile.mkdtemp(prefix="sentinel_test_")
    try:
        test_key_paths()
        test_zip_fingerprint_and_structure_eval(tmp)
        test_stat_eval()
        test_load_raw_interception()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
