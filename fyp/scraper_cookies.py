"""Back-compat alias for fyp.scrape.scraper_cookies — both paths are the same module object."""
import sys

from fyp.scrape import scraper_cookies as _real

sys.modules[__name__] = _real
