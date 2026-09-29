"""Account and administration routes: one submodule per area.

``login`` (sign-in, sign-up, email verification, logout), ``admin_users``,
``admin_roles``, ``admin_site`` (site settings, the hashtag stoplist, the
annotation-version listing) and ``user`` (a user's own settings and profile).
All register on the single ``auth_bp`` blueprint, and endpoint names equal
view-function names, so ``url_for("auth_bp.<view>")`` does not depend on
which submodule defines a view.
"""

from ._blueprint import auth_bp  # noqa: F401

# Importing the submodules registers their routes on auth_bp, in this order.
# isort: off
from . import (  # noqa: E402,F401
    login,
    admin_users,
    admin_roles,
    admin_site,
    user,
)
# isort: on
