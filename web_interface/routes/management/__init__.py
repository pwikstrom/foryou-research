"""Management routes package (Phase 7b split of management_routes.py).

All submodules register their view functions on the single shared
``management_bp`` blueprint; endpoint names equal view-function names, so
``url_for`` targets are unchanged from the pre-split module.
"""

from ._blueprint import management_bp  # noqa: F401

# Importing the submodules registers their routes on management_bp, in the
# same order the routes appeared in the pre-split module.
from . import (  # noqa: E402,F401
    studies,
    collections,
    enrichment,
    contracts,
    ab_eval,
    schema,
    ingestion,
)
