"""Back-compat alias for fyp.scrape.scrape_versioning — both paths are the same module object."""
import sys

from fyp.scrape import scrape_versioning as _real

sys.modules[__name__] = _real
