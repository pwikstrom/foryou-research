"""One-off rewrites of the stored activity data.

Each module here is the testable half of a ``scripts/migrate_*.py`` CLI: pure
functions over the loaded frame, with storage reads injectable so a unit test
can drive them with synthetic data.
"""
