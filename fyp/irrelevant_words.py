"""Back-compat alias for fyp.annotation.irrelevant_words — both paths are the same module object."""
import sys

from fyp.annotation import irrelevant_words as _real

sys.modules[__name__] = _real
