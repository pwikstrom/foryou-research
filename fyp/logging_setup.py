"""Back-compat alias for fyp.core.logging_setup — both paths are the same module object."""
import sys

from fyp.core import logging_setup as _real

sys.modules[__name__] = _real
