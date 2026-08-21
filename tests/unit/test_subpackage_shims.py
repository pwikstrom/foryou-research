"""Phase 8 guard: subpackage moves must be invisible at the old import paths.

Three failure modes this pins, none of which the other gates can catch:

1. **Alias identity** — every renamed module's old path must be the *same
   module object* as its new location (sys.modules alias shim). Tests patch
   fyp modules by attribute assignment (``data_io.load_json = fake``); a
   plain re-export shim would split-brain those patches.
2. **Shim poisoning** — while an old-path shim is partially initialized, an
   import cascade that re-enters the same old path (e.g. boot →
   var_presentation → ``fyp.scrape_contract`` shim → ``fyp/scrape/__init__``
   → ``scrape.py`` importing ``fyp.scrape_contract`` again) binds the *empty
   shim object* via CPython's circular-import fallback. Import and boot
   succeed; every scrape call dies later with AttributeError. The functional
   probes below run in fresh interpreters with adversarial import orders and
   actually *call through* the bindings.
3. **Registration order** — ``fyp/ingest/`` gets an eager ``__init__``; the
   collection-registry order (and thus ``registered_raw_locations()``) must
   stay byte-identical.

Each ordering runs in its own interpreter (same pattern as
tests/unit/test_import_cycle_hash.py).

Usage:
    python -m pytest tests/unit/test_subpackage_shims.py
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Planned old-path -> new-path aliases. Identity is asserted only once the
# new location exists, so this test passes at every commit of the migration.
ALIASED_MOVES = {
    # core
    "fyp.fyp_config": "fyp.core.fyp_config",
    "fyp.data_io": "fyp.core.data_io",
    "fyp.types": "fyp.core.types",
    "fyp.utils": "fyp.core.utils",
    "fyp.logging_setup": "fyp.core.logging_setup",
    "fyp.polars_ops": "fyp.core.polars_ops",
    "fyp.media_paths": "fyp.core.media_paths",
    "fyp.registry_metadata": "fyp.core.registry_metadata",
    "fyp.activity_contract": "fyp.core.activity_contract",
    "fyp.activity_versioning": "fyp.core.activity_versioning",
    "fyp.derived_contract": "fyp.core.derived_contract",
    "fyp.structure_sentinel": "fyp.core.structure_sentinel",
    # scrape (satellites; fyp.scrape itself becomes a package, not an alias)
    "fyp.platform_scraper": "fyp.scrape.platform_scraper",
    "fyp.tiktok_dl": "fyp.scrape.tiktok_dl",
    "fyp.instagram_dl": "fyp.scrape.instagram_dl",
    "fyp.youtube_dl": "fyp.scrape.youtube_dl",
    "fyp.scraper_cookies": "fyp.scrape.scraper_cookies",
    "fyp.scrape_queues": "fyp.scrape.scrape_queues",
    "fyp.scrape_contract": "fyp.scrape.scrape_contract",
    "fyp.scrape_versioning": "fyp.scrape.scrape_versioning",
    # annotation
    "fyp.machine_annotation": "fyp.annotation.machine_annotation",
    "fyp.machine_annotation_batch": "fyp.annotation.machine_annotation_batch",
    "fyp.annotation_contract": "fyp.annotation.annotation_contract",
    "fyp.annotation_schema": "fyp.annotation.annotation_schema",
    "fyp.annotation_versioning": "fyp.annotation.annotation_versioning",
    "fyp.ab_eval": "fyp.annotation.ab_eval",
    "fyp.human_eval": "fyp.annotation.human_eval",
    "fyp.recode_variables": "fyp.annotation.recode_variables",
    "fyp.var_presentation": "fyp.annotation.var_presentation",
    "fyp.irrelevant_words": "fyp.annotation.irrelevant_words",
    # analysis
    "fyp.pca": "fyp.analysis.pca",
    "fyp.stats": "fyp.analysis.stats",
    "fyp.embeddings": "fyp.analysis.embeddings",
    "fyp.video_map": "fyp.analysis.video_map",
    "fyp.niche_detection": "fyp.analysis.niche_detection",
    "fyp.session_profile": "fyp.analysis.session_profile",
    "fyp.sequence_analysis": "fyp.analysis.sequence_analysis",
    "fyp.sequence_model": "fyp.analysis.sequence_model",
    "fyp.timeline_analysis": "fyp.analysis.timeline_analysis",
    "fyp.activity_analysis": "fyp.analysis.activity_analysis",
    "fyp.calc_collection_stats": "fyp.analysis.calc_collection_stats",
    "fyp.studies": "fyp.analysis.studies",
    "fyp.organize_datasets": "fyp.analysis.organize_datasets",
    "fyp.donations": "fyp.analysis.donations",
}

# Functional probe: exercises exactly the bindings that shim poisoning would
# corrupt. get_scraper() constructs a BaseScraper, whose __init__ calls
# scrape_contract.load_contract() through the module binding; the scrape
# module attributes route through the (future) package __init__ re-exports
# and its __getattr__; queue_filename() reads the contract via scrape_queues'
# lazy accessor. All of these raise if any binding is an empty shim.
SCRAPE_PROBE = (
    "from fyp.platform_scraper import get_scraper;"
    "s = get_scraper();"
    "import fyp.scrape as sc_mod;"
    "assert callable(sc_mod.check_existing_media);"
    "assert isinstance(sc_mod.CIRCUIT_BREAKER_THRESHOLD, int);"
    "assert isinstance(sc_mod.SCRAPES_LABEL, str);"
    "import fyp.scrape_queues as sq;"
    "assert sq.queue_filename(sq.default_platform());"
    "import fyp.scrape_versioning as sv;"
    "assert callable(sv.ensure_active_version_registered);"
    "print('PROBE_OK')"
)

# Import orders that historically (or by construction) interleave shims with
# the boot cascade. Each runs before the probe in a fresh interpreter.
ADVERSARIAL_PRELUDES = {
    "clean": "",
    "scrape_contract_first": "import fyp.scrape_contract;",
    "scrape_versioning_first": "import fyp.scrape_versioning;",
    "machine_annotation_first": "import fyp.machine_annotation;",
    "var_presentation_first": "import fyp.var_presentation;",
    "boot_first": "from fyp.fyp_config import fyp_cf; _ = fyp_cf['misc'];",
}

EXPECTED_RAW_LOCATIONS = (
    "ddp_raw",
    "aio_raw",
    "zeeschuimer_raw",
    "instagram_raw",
    "youtube_raw",
)





def _run_probe(code: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 0, f"probe failed:\n{code}\n--- stderr ---\n{out.stderr[-3000:]}"
    return out.stdout





def test_alias_identity() -> None:
    """Old and new paths must be the same module object (once moved)."""
    checked = 0
    for old, new in ALIASED_MOVES.items():
        try:
            new_mod = importlib.import_module(new)
        except ModuleNotFoundError:
            continue  # not moved yet — migration is incremental
        old_mod = importlib.import_module(old)
        assert old_mod is new_mod, (
            f"{old} is not the same module object as {new} — the alias shim "
            "is missing or was replaced by a re-export (attribute patching "
            "in tests would split-brain)"
        )
        checked += 1
    print(f"alias identity checked for {checked} moved module(s)")





def test_scrape_bindings_survive_adversarial_import_orders() -> None:
    """No import order may leave a scrape-family binding poisoned."""
    for name, prelude in ADVERSARIAL_PRELUDES.items():
        stdout = _run_probe(prelude + SCRAPE_PROBE)
        assert "PROBE_OK" in stdout, f"{name}: probe produced no PROBE_OK\n{stdout[-1000:]}"





def test_ingest_registration_order_pinned() -> None:
    """Collection registry (and upload-location) order must not change."""
    stdout = _run_probe(
        "import fyp.ingest as ing;"
        "print('LOCS=' + ','.join(ing.registered_raw_locations()))"
    )
    line = next(ln for ln in stdout.splitlines() if ln.startswith("LOCS="))
    got = tuple(line.removeprefix("LOCS=").split(","))
    assert got == EXPECTED_RAW_LOCATIONS, (
        f"registered_raw_locations() order changed: {got} != {EXPECTED_RAW_LOCATIONS}"
    )
