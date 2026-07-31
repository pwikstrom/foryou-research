"""Contract + determinism tests for scripts/generate_demo_dataset.py.

The generator must be deterministic (seeded), emit ids under the demo
prefix, keep donors' per-second timestamp sets disjoint (the ingest-side
same-content clustering would otherwise merge donors), clear the PCA group
floor, and produce scrape/annotation artifacts the real pipeline accepts.
"""

import itertools
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import generate_demo_dataset as gen  # noqa: E402

from fyp.core.utils import DEMO_ITEM_ID_PREFIX  # noqa: E402


@pytest.fixture(scope="module")
def result():
    return gen.generate(seed=123, donors=3, days=20, annotation_version="av_test")


def _play_ts_sets(result):
    out = {}
    for fn, doc in result["donor_files"].items():
        out[fn] = {r["Date"] for r in doc["Activity"]["Video Browsing History"]["VideoList"]}
    return out


def test_determinism_same_seed_same_output(result):
    again = gen.generate(seed=123, donors=3, days=20, annotation_version="av_test")
    assert result["donor_files"] == again["donor_files"]
    assert result["annotation_entries"] == again["annotation_entries"]
    assert result["scrape_raw"].equals(again["scrape_raw"])

    different = gen.generate(seed=124, donors=3, days=20, annotation_version="av_test")
    assert different["donor_files"] != result["donor_files"]


def test_item_ids_carry_demo_prefix(result):
    for it in result["items"]:
        assert it["item_id"].startswith(DEMO_ITEM_ID_PREFIX)
        assert len(it["item_id"]) == 19 and it["item_id"].isdigit()
    ids = result["scrape_raw"]["item_id"]
    assert ids.str.startswith(DEMO_ITEM_ID_PREFIX).all()


def test_donor_timestamp_sets_are_disjoint(result):
    ts_sets = _play_ts_sets(result)
    for a, b in itertools.combinations(ts_sets, 2):
        assert not (ts_sets[a] & ts_sets[b]), (a, b)


def test_ddp_format_contract(result):
    """Every play row has Date first, Link second, trailing slash, valid ts."""
    for fn, doc in result["donor_files"].items():
        video_list = doc["Activity"]["Video Browsing History"]["VideoList"]
        assert len(video_list) > 10, fn  # the >=11-videolist-rows ingest floor
        for row in video_list[:50]:
            keys = list(row.keys())
            assert keys[0] == "Date" and keys[1] == "Link"
            assert row["Link"].endswith("/")
            item_id = row["Link"].rsplit("/", 2)[-2]
            assert item_id.isdigit() and len(item_id) == 19


def test_pca_group_floor(result):
    """>=10 (collection, local_date) groups with >=10 annotated plays each."""
    annotated = {e["item_id"] for e in result["annotation_entries"].values()}
    qualifying = 0
    for fn, doc in result["donor_files"].items():
        rows = doc["Activity"]["Video Browsing History"]["VideoList"]
        per_day = Counter(
            r["Date"][:10] for r in rows
            if r["Link"].rsplit("/", 2)[-2] in annotated
        )
        qualifying += sum(1 for n in per_day.values() if n >= 10)
    assert qualifying >= 10


def test_scrape_rows_canonicalize_to_full_contract(result):
    from fyp.scrape.platform_scraper import get_scraper
    from fyp.scrape.scrape_contract import base_field_names, load_contract

    scraper = get_scraper("tiktok")
    frame = scraper.prepare_raw_batch(result["scrape_raw"].copy())
    canonical = scraper.canonicalize_batch(frame, status="ok")

    for col in base_field_names(load_contract()):
        assert col in canonical.columns, col
    assert (canonical["scrape_status"] == "ok").all()
    assert canonical["scrape_contract_version"].str.startswith("sv_").all()
    assert (~canonical["video_downloaded"].fillna(False)).all()
    assert canonical["faves_per_K_play"].notna().all()
    assert canonical["plays_per_day"].notna().all()


def test_annotation_responses_flatten_cleanly(result):
    from fyp.annotation.annotation_schema import flatten_structured
    from fyp.annotation.annotation_contract import load_contract

    contract = load_contract()
    enums = contract["enums"]
    category_values = set(enums["content_category"])

    for entry in list(result["annotation_entries"].values())[:100]:
        assert entry["structured"] is True
        response = json.loads(entry["response"])
        flat = flatten_structured(response)
        # type_of_story drives annotated_ok downstream — must never be null.
        assert flat["type_of_story"] in set(enums["type_of_story"])
        for cat in response["content_category"]:
            assert cat in category_values
        assert response["main_gender"] in set(enums["gender"])
        assert response["main_ethnicity"] in set(enums["ethnicity"])
        assert 0 <= response["political_score"] <= 100
        assert 0 <= response["sensitivity_score"] <= 100
        assert "audio_summary_speech_vs_music" in flat


def test_demo_collection_ingests_donor_file(result, monkeypatch):
    """One donor file survives the REAL demo-collection parse end to end."""
    import fyp.ingest.tiktok as tiktok_mod

    fn, doc = next(iter(result["donor_files"].items()))
    monkeypatch.setattr(tiktok_mod.data_io, "load_json",
                        lambda storage_location=None, filename=None, **kw: doc)

    sub = tiktok_mod.TikTokDemoCollection(verbose=False)
    assert sub.data_source == "demo"
    assert sub.raw_path == "demo_raw"

    raw = sub.load_single_raw(fn)
    assert len(raw) > 10
    raw["raw_file"] = fn
    processed = sub.process_single(raw)

    plays = processed[processed["activity_type"] == "play"]
    assert len(plays) > 10
    assert plays["item_id"].str.startswith(DEMO_ITEM_ID_PREFIX).all()
    assert plays["utc_timestamp"].notna().all()
    # Dwell derived from the generated inter-play gaps must be plausible.
    dwell = plays["play_duration"].dropna()
    assert len(dwell) > 0
    assert (dwell >= 0).all() and (dwell <= 600).all()


def test_embeddings_backlog_excludes_demo_items(monkeypatch):
    import pandas as pd

    import fyp.analysis.embeddings as emb

    df = pd.DataFrame({
        "item_id": ["7000000000000000001", DEMO_ITEM_ID_PREFIX + "0" * 15],
        "annotated_ok": [True, True],
    })
    monkeypatch.setattr(emb.data_io, "exists", lambda **kw: True)
    monkeypatch.setattr(emb.data_io, "load_parquet_selective", lambda **kw: df)
    assert emb.annotated_ok_item_ids() == ["7000000000000000001"]
