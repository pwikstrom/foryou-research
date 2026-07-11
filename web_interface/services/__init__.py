"""Shared service-layer modules for the web interface.

Non-route business logic extracted from the route modules (Phase 7). Route
modules and workers import from here; nothing in this package may import a
route module or ``auth`` (the route layer sits above the service layer).
"""
