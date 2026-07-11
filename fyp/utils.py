"""Back-compat alias for fyp.core.utils — both paths are the same module object."""
import sys

from fyp.core import utils as _real

sys.modules[__name__] = _real
