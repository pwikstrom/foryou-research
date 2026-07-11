"""Back-compat alias for fyp.analysis.organize_datasets — both paths are the same module object."""
import sys

from fyp.analysis import organize_datasets as _real

sys.modules[__name__] = _real
