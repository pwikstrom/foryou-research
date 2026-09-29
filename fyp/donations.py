"""Back-compat alias for fyp.ingest.donations — both paths are the same module object."""

import sys

from fyp.ingest import donations as _real

sys.modules[__name__] = _real
