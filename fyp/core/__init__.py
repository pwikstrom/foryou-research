"""Core infrastructure: config, I/O, dtypes, logging, and shared contracts.

This package ``__init__`` is deliberately inert (no imports): several members
(``data_io``, the versioning/contract modules) are imported *mid-boot* by
``fyp_config.load_var_schema`` and must not trigger any eager work.
"""
