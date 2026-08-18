"""Pytest configuration for the For You Data Hub test suite.

Ensures the project root is importable regardless of the directory pytest is
invoked from (mirrors the ``sys.path`` bootstrap used by the worker scripts),
so ``import fyp`` and ``import web_interface`` resolve without installing the
package.

Markers (registered in ``pyproject.toml``):
    requires_data: needs local/production data files not in a fresh checkout.
    requires_gcs:  needs live GCS / GCP credentials.
    slow:          long-running; excluded from the quick gate.

The quick, checkout-only gate is::

    pytest -m "not requires_data and not requires_gcs and not slow"
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests._storage_guard import assert_local_storage  # noqa: E402

# Module level, not a sessionstart hook: the root conftest loads before any
# per-directory conftest can boot the config, so a GCS-resolving environment
# is refused before anything touches data_io.
assert_local_storage()
