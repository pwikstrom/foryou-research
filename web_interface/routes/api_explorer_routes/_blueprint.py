"""The Explore and admin-system blueprint object.

Lives in its own module so every route submodule can import it without
importing the package __init__ (which imports the submodules).
"""

from flask import Blueprint

explorer_bp = Blueprint("explorer_bp", __name__)
