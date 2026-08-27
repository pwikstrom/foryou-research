"""The For You Data Hub — short-video research data pipeline.

Ingestion of TikTok/Instagram/YouTube feed activity and data donations,
web-scrape and LLM-annotation enrichment, and statistical analysis.

Deliberately import-free: importing fyp submodules here would trigger
fyp_config's import-time initialization and violate the import-cycle rule
(see CONTRIBUTING.md; guarded by tests/unit/test_import_cycle_hash.py).
"""

__version__ = "0.2.0"
