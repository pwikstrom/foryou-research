"""Back-compat alias for fyp.core.types — both paths are the same module object."""
import sys

from fyp.core import types as _real

sys.modules[__name__] = _real
