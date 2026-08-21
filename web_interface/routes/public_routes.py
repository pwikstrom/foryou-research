"""Public (unauthenticated) mini-site pages.

These routes are deliberately import-light — Flask only, no fyp imports — so
registering them never touches config loading and cannot participate in the
fyp_config import cycle. All page content is hardcoded in the templates under
``templates/public/``.
"""

from flask import Blueprint, redirect, render_template, url_for

public_bp = Blueprint('public_bp', __name__)


@public_bp.route('/about')
def about():
    """The For You Research Project: questions, methods, funding, team."""
    return render_template('public/about.html', active_page='about')


@public_bp.route('/participate')
def participate():
    """For TikTok users and creators: why and how to take part in the research."""
    return render_template('public/participate.html', active_page='participate')


@public_bp.route('/data-donation')
def data_donation():
    """Explainer: what data donation is, why donated feeds, what they can tell us."""
    return render_template('public/data_donation.html', active_page='data_donation')


@public_bp.route('/thehub')
def thehub():
    """The For You Data Hub: hosted access, tool tour, pipeline, self-hosting."""
    return render_template('public/thehub.html', active_page='thehub')


@public_bp.route('/guide')
def guide():
    """Old name for the Data Hub page; kept as a redirect for stale links."""
    return redirect(url_for('public_bp.thehub'), code=301)


@public_bp.route('/our-team')
def our_team():
    """Old Wix-site path; foryouresearch.net now serves the hub directly."""
    return redirect(url_for('public_bp.about'), code=301)


@public_bp.route('/be-a-citizen-scientist')
def be_a_citizen_scientist():
    """Old Wix-site path; foryouresearch.net now serves the hub directly."""
    return redirect(url_for('public_bp.participate'), code=301)


@public_bp.route('/faq')
def faq():
    """Frequently asked questions about the Data Hub and the project."""
    return render_template('public/faq.html', active_page='faq')
