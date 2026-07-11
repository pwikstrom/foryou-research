"""Back-compat alias for fyp.annotation.human_eval — both paths are the same module object."""
import sys

from fyp.annotation import human_eval as _real

sys.modules[__name__] = _real
