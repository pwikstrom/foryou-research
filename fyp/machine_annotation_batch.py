"""Back-compat alias for fyp.annotation.machine_annotation_batch — both paths are the same module object."""
import sys

from fyp.annotation import machine_annotation_batch as _real

sys.modules[__name__] = _real
