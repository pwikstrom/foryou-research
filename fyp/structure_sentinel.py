"""Back-compat alias for fyp.core.structure_sentinel — both paths are the same module object."""
import sys

from fyp.core import structure_sentinel as _real

sys.modules[__name__] = _real
