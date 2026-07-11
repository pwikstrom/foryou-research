"""Back-compat alias for fyp.core.data_io — both paths are the same module object."""
import sys

from fyp.core import data_io as _real

sys.modules[__name__] = _real
