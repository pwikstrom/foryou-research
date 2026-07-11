"""Back-compat alias for fyp.core.media_paths — both paths are the same module object."""
import sys

from fyp.core import media_paths as _real

sys.modules[__name__] = _real
