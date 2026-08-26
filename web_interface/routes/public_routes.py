"""Public (unauthenticated) mini-site pages.

These routes are deliberately import-light — Flask and ``web_interface.seo``,
no fyp imports — so registering them never touches config loading and cannot
participate in the fyp_config import cycle. ``seo`` holds to the same rule and
reads config lazily, inside the request. All page content is hardcoded in the
templates under ``templates/public/``.
"""

from flask import Blueprint, Response, redirect, render_template, url_for

from web_interface import seo

public_bp = Blueprint('public_bp', __name__)


@public_bp.route('/about')
def about():
    """The For You Research Project: questions, methods, funding, team."""
    return render_template('public/about.html', active_page='about')


@public_bp.route('/participate')
def participate():
    """For TikTok users and creators: why and how to take part in the research."""
    return render_template('public/participate.html', active_page='participate')


@public_bp.route('/participate/start')
def participate_start():
    """Three-stage participation wizard: request data, wait, upload.

    Stage selection is entirely client-side (``?stage=`` plus localStorage)
    so the route stays static, cacheable and deep-linkable.
    """
    return render_template('public/participate_start.html', active_page='participate')


@public_bp.route('/participate/go-upload')
def participate_go_upload():
    """Land a logged-in participant on My Collections with the upload open.

    Used as the ``?next=`` target of the wizard's stage-3 login link: URL
    fragments don't survive a form-posted login, so login redirects here and
    this route 302s to the app shell's hash contract (see index.html).
    """
    return redirect('/#my_stuff/my-collections/upload')


@public_bp.route('/participate/go-tour')
def participate_go_tour():
    """Start the in-app guided tour: redirect to the app shell's #tour hash.

    Same fragment-preserving trick as ``participate_go_upload`` — used as the
    stage-2 login/signup ``?next=`` target.
    """
    return redirect('/#tour')


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


@public_bp.route('/terms')
def terms():
    """Terms of use for a Hub account — linked from the signup checkbox.

    Account terms only; the research-participation consent statement stays in
    the donation upload flow, where sharing actually happens.
    """
    return render_template('public/terms.html', active_page=None)


@public_bp.route('/faq')
def faq():
    """Frequently asked questions about the Data Hub and the project."""
    return render_template('public/faq.html', active_page='faq')


@public_bp.route('/robots.txt')
def robots():
    """Crawl rules, plus the pointer that makes the sitemap discoverable.

    Served from the app rather than a static file so the ``Sitemap:`` line
    carries this instance's own hostname — a static file would hardcode one
    operator's domain into every fork.
    """
    return Response(seo.robots_txt(), mimetype='text/plain')


@public_bp.route('/sitemap.xml')
def sitemap():
    """The canonical URL of every public page, for Search Console to ingest."""
    return Response(seo.sitemap_xml(), mimetype='application/xml')
