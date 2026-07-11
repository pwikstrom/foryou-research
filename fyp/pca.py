"""Back-compat alias for fyp.analysis.pca — both paths are the same module object."""
import sys

from fyp.analysis import pca as _real

sys.modules[__name__] = _real
