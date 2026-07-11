"""Back-compat alias for fyp.annotation.annotation_versioning — both paths are the same module object."""
import sys

from fyp.annotation import annotation_versioning as _real

sys.modules[__name__] = _real
