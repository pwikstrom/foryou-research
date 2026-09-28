"""Shared service-layer modules for the web interface.

Non-route business logic used by the route modules and workers, which import
from here; nothing in this package may import a route module or ``auth`` (the
route layer sits above the service layer).
"""
