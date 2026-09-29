"""Shared service-layer modules for the web interface.

Non-route business logic used by the route modules and workers, which import
from here. Nothing in this package may import a route module or the Flask-facing
auth modules (``auth.security``, ``auth.permissions``) — the route layer sits
above the service layer. The account store, ``auth.accounts``, is storage and
may be used.
"""
