"""Back-compat alias for fyp.analysis.session_profile — both paths are the same module object."""
import sys

from fyp.analysis import session_profile as _real

sys.modules[__name__] = _real
