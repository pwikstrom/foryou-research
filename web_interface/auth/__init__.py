"""Accounts and access control.

``accounts`` is the user and role store (JSON records under
``{local_data}/users/``) and its ``user_manager`` singleton; ``security`` wires
it into Flask-Login; ``permissions`` is the tab/sub-page permission catalog and
the route guards (``permission_required``, ``admin_required``);
``email_verification`` issues and checks sign-up verification links.

Services may use ``accounts`` (it is storage), but not the Flask-facing
modules — the route layer sits above the service layer.
"""
