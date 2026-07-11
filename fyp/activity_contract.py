"""Back-compat alias for fyp.core.activity_contract — both paths are the same module object."""
import sys

from fyp.core import activity_contract as _real

sys.modules[__name__] = _real
