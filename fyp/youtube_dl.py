"""Back-compat alias for fyp.scrape.youtube_dl — both paths are the same module object."""
import sys

from fyp.scrape import youtube_dl as _real

sys.modules[__name__] = _real
