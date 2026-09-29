"""The account and administration blueprint object.

Lives in its own module so every route submodule can import it without
importing the package __init__ (which imports the submodules).
"""

from flask import Blueprint

auth_bp = Blueprint("auth_bp", __name__)
