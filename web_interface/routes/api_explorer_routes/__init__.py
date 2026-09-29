"""Explore-tab and admin system routes, on the single ``explorer_bp`` blueprint.

``explore`` serves study definitions, methods notes, metadata and filtering;
``system`` the admin System Information, System Health and daily ops report
endpoints. Endpoint names equal view-function names, so
``url_for("explorer_bp.<view>")`` does not depend on which submodule defines
a view.
"""

from ._blueprint import explorer_bp  # noqa: F401

# Importing the submodules registers their routes on explorer_bp, in this order.
# isort: off
from . import (  # noqa: E402,F401
    explore,
    system,
)
# isort: on
