"""Back-compat alias for fyp.core.memory — both paths are the same module object."""
import sys

from fyp.core import memory as _real

sys.modules[__name__] = _real
