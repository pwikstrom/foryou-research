"""Back-compat alias for fyp.analysis.donations — both paths are the same module object."""
import sys

from fyp.analysis import donations as _real

sys.modules[__name__] = _real
