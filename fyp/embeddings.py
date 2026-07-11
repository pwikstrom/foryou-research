"""Back-compat alias for fyp.analysis.embeddings — both paths are the same module object."""
import sys

from fyp.analysis import embeddings as _real

sys.modules[__name__] = _real
