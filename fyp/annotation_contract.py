"""Back-compat alias for fyp.annotation.annotation_contract — both paths are the same module object."""
import sys

from fyp.annotation import annotation_contract as _real

sys.modules[__name__] = _real
