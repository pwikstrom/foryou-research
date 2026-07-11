"""Back-compat alias for fyp.annotation.annotation_schema — both paths are the same module object."""
import sys

from fyp.annotation import annotation_schema as _real

sys.modules[__name__] = _real
