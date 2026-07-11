"""Back-compat alias for fyp.annotation.recode_variables — both paths are the same module object."""
import sys

from fyp.annotation import recode_variables as _real

sys.modules[__name__] = _real
