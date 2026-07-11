"""Back-compat alias for fyp.scrape.scrape_queues — both paths are the same module object."""
import sys

from fyp.scrape import scrape_queues as _real

sys.modules[__name__] = _real
