"""Back-compat alias for fyp.analysis.niche_detection — both paths are the same module object."""
import sys

from fyp.analysis import niche_detection as _real

sys.modules[__name__] = _real
