"""The For You Data Hub — short-video research data pipeline.

Ingestion of TikTok/Instagram/YouTube feed activity and data donations,
web-scrape and LLM-annotation enrichment, and statistical analysis.

Deliberately import-free: ``import fyp`` must stay cheap and side-effect
free. Config boots lazily on first access to ``fyp_config.fyp_cf`` (some
submodules, e.g. ``fyp.ingest``, trigger that boot when imported), and eager
submodule imports here would also risk the import-cycle rule (see
CONTRIBUTING.md invariant 1; guarded by tests/unit/test_import_cycle_hash.py
and tests/unit/test_lazy_config_boot.py).
"""

__version__ = "0.4.1"
