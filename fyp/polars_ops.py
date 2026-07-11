"""Back-compat alias for fyp.core.polars_ops — both paths are the same module object."""
import sys

from fyp.core import polars_ops as _real

sys.modules[__name__] = _real
