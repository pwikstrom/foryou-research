"""Back-compat alias for fyp.analysis.video_map — both paths are the same module object."""
import sys

from fyp.analysis import video_map as _real

sys.modules[__name__] = _real
