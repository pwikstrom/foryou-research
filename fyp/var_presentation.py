"""Back-compat alias for fyp.annotation.var_presentation — both paths are the same module object."""
import sys

from fyp.annotation import var_presentation as _real

sys.modules[__name__] = _real
