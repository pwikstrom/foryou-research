"""Back-compat alias for fyp.scrape.scrape_contract — both paths are the same module object."""
import sys

from fyp.scrape import scrape_contract as _real

sys.modules[__name__] = _real
