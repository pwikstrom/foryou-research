"""Back-compat alias for fyp.core.derived_contract — both paths are the same module object."""
import sys

from fyp.core import derived_contract as _real

sys.modules[__name__] = _real
