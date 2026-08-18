"""Statistical analysis, dataset organisation, and study-level metrics.

This package ``__init__`` is deliberately inert (no imports): its members
must stay lazy w.r.t. the config boot, and ``organize_datasets`` carries a
module ``__getattr__`` for lazy config-derived constants.
"""
