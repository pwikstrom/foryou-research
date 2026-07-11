"""Collection triage for tests/unit (2026-07 baseline).

Files listed in ``collect_ignore`` cannot currently be collected by pytest at
all, so markers can't reach them. They fall into two groups. None of this
affects running them directly (``python tests/unit/<file>.py``).

Import-time failures (data- or drift-dependent module-level code):
  - test_annotate_calc.py      KeyError 'study_defs' at import (needs live study defs)
  - test_bbc_jacqui_stats.py   unpack error at import (stale study data shape)
  - test_calc.py               study 'paper_one' no longer exists
  - test_fillna.py             bool fillna on int64[pyarrow] — pandas/pyarrow drift
  - test_rename_keys.py        imports removed module 'rename_keys_and_columns'
  - test_timeline_analysis.py  needs recoded/ddp_metadata.parquet on disk

Self-runner integration scripts (own ``main()`` harness, expect a ``client``
argument that is not a pytest fixture; they also snapshot/restore live
var-schema and presentation stores, so they must not run in a shared gate):
  - test_annotation_contract_api.py
  - test_annotation_contract_editor.py
  - test_var_schema_api.py

When one of these is fixed or converted to proper pytest style, delete its
entry here.

A second, conditional group runs pipeline code against the live local corpus
at module import (so ``requires_data`` markers can't reach them either); they
are collected only when local data is actually present, and ignored on a
fresh checkout / CI.
"""

import glob
import os

collect_ignore = [
    "test_annotate_calc.py",
    "test_bbc_jacqui_stats.py",
    "test_calc.py",
    "test_fillna.py",
    "test_rename_keys.py",
    "test_timeline_analysis.py",
    "test_annotation_contract_api.py",
    "test_annotation_contract_editor.py",
    "test_var_schema_api.py",
]

# Import-time data-dependent files: need recoded parquets on local disk.
_DATA_DEPENDENT = [
    "test_zee_generic_fix.py",
    "test_zee_generic_step.py",
]


def _local_corpus_present() -> bool:
    """True when the configured local recoded store holds any parquet."""
    from fyp.fyp_config import fyp_cf

    recoded = fyp_cf["paths"].get("recoded", "")
    return bool(recoded) and bool(glob.glob(os.path.join(recoded, "*.parquet")))


if not _local_corpus_present():
    collect_ignore += _DATA_DEPENDENT
