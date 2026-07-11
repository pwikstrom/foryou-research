"""Back-compat alias for fyp.core.activity_versioning — both paths are the same module object."""
import sys

from fyp.core import activity_versioning as _real

sys.modules[__name__] = _real
