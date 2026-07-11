"""Back-compat alias for fyp.analysis.sequence_model — both paths are the same module object."""
import sys

from fyp.analysis import sequence_model as _real

sys.modules[__name__] = _real
