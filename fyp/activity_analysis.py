"""Back-compat alias for fyp.analysis.activity_analysis — both paths are the same module object."""
import sys

from fyp.analysis import activity_analysis as _real

sys.modules[__name__] = _real
