"""Back-compat alias for fyp.core.registry_metadata — both paths are the same module object."""
import sys

from fyp.core import registry_metadata as _real

sys.modules[__name__] = _real
