"""Back-compat alias for fyp.scrape.tiktok_dl — both paths are the same module object."""
import sys

from fyp.scrape import tiktok_dl as _real

sys.modules[__name__] = _real
