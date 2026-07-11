"""Back-compat alias for fyp.analysis.calc_collection_stats — both paths are the same module object."""
import sys

from fyp.analysis import calc_collection_stats as _real

sys.modules[__name__] = _real
