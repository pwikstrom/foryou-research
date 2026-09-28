"""Management routes package: one submodule per admin domain.

All submodules register their view functions on the single shared
``management_bp`` blueprint; endpoint names equal view-function names, so
``url_for("management.<view>")`` targets do not depend on which submodule
defines a view.
"""

from ._blueprint import management_bp  # noqa: F401

# Importing the submodules registers their routes on management_bp. The order
# is the blueprint's route registration order, kept stable on purpose; the
# isort fence keeps the import sorter from alphabetising them.
# isort: off
from . import (  # noqa: E402,F401
    studies,
    collections,
    enrichment,
    contracts,
    ab_eval,
    schema,
    data_contracts,
    ingestion,
)
# isort: on
