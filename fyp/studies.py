"""Back-compat alias for fyp.analysis.studies — both paths are the same module object."""
import sys

from fyp.analysis import studies as _real

sys.modules[__name__] = _real
