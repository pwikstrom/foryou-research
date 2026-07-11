"""Back-compat alias for fyp.scrape.instagram_dl — both paths are the same module object."""
import sys

from fyp.scrape import instagram_dl as _real

sys.modules[__name__] = _real
