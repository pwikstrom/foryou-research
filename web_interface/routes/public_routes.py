"""Public (unauthenticated) mini-site pages.

These routes are deliberately import-light — Flask only, no fyp imports — so
registering them never touches config loading and cannot participate in the
fyp_config import cycle. All page content is hardcoded in the templates under
``templates/public/``.
"""

from flask import Blueprint, render_template

public_bp = Blueprint('public_bp', __name__)


@public_bp.route('/about')
def about():
    """The For You Research Project: questions, methods, funding, team."""
    return render_template('public/about.html', active_page='about')


@public_bp.route('/guide')
def guide():
    """How to use the Data Hub: getting access + first-session walkthrough."""
    return render_template('public/guide.html', active_page='guide')


@public_bp.route('/faq')
def faq():
    """Frequently asked questions about the Data Hub and the project."""
    return render_template('public/faq.html', active_page='faq')
