"""Back-compat alias for fyp.scrape.platform_scraper — both paths are the same module object."""
import sys

from fyp.scrape import platform_scraper as _real

sys.modules[__name__] = _real
