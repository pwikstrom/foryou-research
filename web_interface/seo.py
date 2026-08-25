"""Search-engine and social-preview metadata for the public mini-site.

Until 2026-08 the public pages shipped no indexing metadata at all: one
hardcoded ``<title>`` for all of them, no canonical link, no ``robots.txt`` and
no sitemap. That was survivable while foryouresearch.net was a Wix site, and
stopped being survivable the moment the domain was pointed at this app. Google
then found byte-identical content on three hosts — the apex, ``www.`` and the
raw Cloud Run URL — with nothing telling it which one to keep. It picked
``www.``, whose last crawl still carried the old Wix ``noindex``, and the whole
domain fell out of the index (Search Console, 2026-08-22: 0 pages indexed, 7
excluded). This module supplies the signals that were missing.

Everything keys off one value, ``[site] app_url`` — the public URL an operator
wants indexed. It is deliberately the only place the canonical hostname is
written down: the templates, the sitemap, the JSON-LD and the ``www.`` redirect
all read it, so a fork repoints the entire surface by setting ``FYP_APP_URL``.
While it is unset the module falls back to the requesting host, which is what a
local or intranet install wants — self-consistent metadata, and no redirect
anywhere.

No ``fyp`` import at module scope, for the same reason ``public_routes`` avoids
one: this is reached from blueprint registration, and must not drag config
loading into the ``fyp_config`` import cycle.
"""

import json
from urllib.parse import urlsplit

from flask import request, url_for

# ---------------------------------------------------------------------------
# Page-level metadata
# ---------------------------------------------------------------------------

# The indexable public pages, keyed by Flask endpoint. This is the single
# source for three things that used to disagree or not exist: the per-page
# <title>, the meta description, and the sitemap's URL list.
#
# Titles stay under ~60 characters and descriptions under ~160 so search
# engines show them whole rather than truncating or rewriting them. Every one
# is a claim the page actually makes — a description that oversells the page
# is worse than none, because it raises the bounce rate that ranking watches.
PUBLIC_PAGES = {
    "index": {
        "title": "For You Research: What Shapes Your For You Feed?",
        "description": (
            "How do TikTok, Instagram Reels and YouTube Shorts decide what you see? "
            "Share your own feed, or use the For You Data Hub to study short-video "
            "culture at scale."
        ),
    },
    "public_bp.participate": {
        "title": "Participate: What Does Your For You Page Say About You?",
        "description": (
            "Share your TikTok data with researchers at QUT and the University of "
            "Sydney, get your own TikTok personality profile, and help decode how "
            "the algorithm works."
        ),
    },
    "public_bp.thehub": {
        "title": "The For You Data Hub: A Short-Video Research Workbench",
        "description": (
            "A browser-based workbench that turns donated TikTok, Instagram and "
            "YouTube feeds into datasets, timelines and semantic maps. Open source, "
            "with hosted access."
        ),
    },
    "public_bp.about": {
        "title": "About the For You Research Project",
        "description": (
            "An ARC-funded study of TikTok's recommendation algorithm and Australian "
            "audiences, based at QUT's Digital Media Research Centre and the "
            "University of Sydney."
        ),
    },
    "public_bp.data_donation": {
        "title": "What Is Data Donation? The Research Method Explained",
        "description": (
            "Data donation lets researchers work from the feed a real person was "
            "actually served, instead of scraped content or platform APIs. Why "
            "donated feeds are different."
        ),
    },
    "public_bp.faq": {
        "title": "For You Data Hub: Frequently Asked Questions",
        "description": (
            "Answers on accounts, supported platforms, participant privacy, AI "
            "annotation reliability, running your own instance of the open-source "
            "Hub, and how to cite it."
        ),
    },
}

# Shown when a page outside PUBLIC_PAGES renders base.html — the app shell,
# login and signup. Those are never indexed, so this only has to be a sane
# browser-tab label.
DEFAULT_TITLE = "For You Data Hub"

SITE_NAME = "The For You Research Project"

# Crawlers may not fetch these, so they never reach the sitemap either. /api
# and /internal are machine endpoints; the auth pages are thin, duplicated
# across every install, and worth nothing in a result page.
DISALLOWED_PATHS = ("/api/", "/internal/", "/login", "/logout", "/signup")


# ---------------------------------------------------------------------------
# Canonical origin
# ---------------------------------------------------------------------------


def configured_origin():
    """Return the operator's canonical origin, or "" when none is configured.

    Reads ``[site] app_url`` (env override ``FYP_APP_URL``) and keeps only the
    scheme and host, so a value with a trailing slash or a stray path still
    yields a clean origin to build absolute URLs from.

    Returns:
        A string like ``https://foryouresearch.net``, or "" when ``app_url`` is
        unset, unparseable, or missing a scheme or host.
    """
    try:
        from fyp.fyp_config import get_config

        app_url = str((get_config().get("site", {}) or {}).get("app_url", "") or "").strip()
    except Exception:
        return ""

    if not app_url:
        return ""
    parts = urlsplit(app_url)
    if not (parts.scheme and parts.netloc):
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def origin():
    """The origin to build absolute URLs from for the current request.

    Falls back to the requesting host when no ``app_url`` is configured, so a
    local or self-hosted instance still emits self-consistent canonical links
    and sitemap entries instead of pointing at somebody else's domain.
    """
    return configured_origin() or request.url_root.rstrip("/")


def absolute(path):
    """Join ``path`` onto the canonical origin."""
    return f"{origin()}{path}"


def canonical_host_redirect():
    """Return a 301 onto the canonical host, or None to serve the request.

    Registered as a ``before_request`` hook. It answers exactly one question —
    "is this the ``www.`` twin of the configured canonical host?" — because
    that was the duplicate Google actually chose over the real site.

    Deliberately narrow. Any *other* unexpected host (the raw Cloud Run URL, a
    staging alias, an IP) is served normally and de-duplicated by the canonical
    ``<link>`` instead, which costs a crawl but cannot strand a request. A
    blanket "redirect anything that is not canonical" rule would also catch
    health checks and internal callers, and would hard-fail an operator who
    sets ``app_url`` to the wrong value.

    Returns:
        A Flask redirect response, or None when the request should proceed.
    """
    if request.method not in ("GET", "HEAD"):
        return None

    canonical = configured_origin()
    if not canonical:
        return None

    canonical_host = urlsplit(canonical).netloc
    if not canonical_host or request.host == canonical_host:
        return None
    if request.host != f"www.{canonical_host}":
        return None

    from flask import redirect

    target = f"{canonical}{request.full_path if request.query_string else request.path}"
    return redirect(target, code=301)


# ---------------------------------------------------------------------------
# robots.txt and sitemap.xml
# ---------------------------------------------------------------------------


def robots_txt():
    """The body of ``/robots.txt``.

    Allows everything the public site is made of — including static CSS and JS,
    which Google needs in order to render a page the way a visitor sees it —
    and withholds only the machine and auth endpoints.

    AI crawlers (GPTBot, ClaudeBot, PerplexityBot, Google-Extended and the
    rest) are covered by the ``*`` group and are therefore allowed. That is a
    deliberate default for a publicly funded project whose public pages carry
    no participant data and whose goal is to be found and cited; the comment in
    the served file says how to reverse it.
    """
    lines = [
        "# The public pages are open to all crawlers, AI crawlers included.",
        "# To opt a specific one out, add a group above the * group, e.g.:",
        "#   User-agent: GPTBot",
        "#   Disallow: /",
        "",
        "User-agent: *",
        "Allow: /",
    ]
    lines += [f"Disallow: {path}" for path in DISALLOWED_PATHS]
    lines += ["", f"Sitemap: {absolute('/sitemap.xml')}", ""]
    return "\n".join(lines)


def sitemap_xml():
    """The body of ``/sitemap.xml``: the canonical URL of every public page.

    Only ``<loc>``. Google ignores ``<priority>`` and ``<changefreq>``, and a
    ``<lastmod>`` this app cannot compute honestly is worse than none — a
    sitemap that claims every page changed today gets its dates disregarded
    wholesale.
    """
    locs = "\n".join(
        f"  <url><loc>{absolute(url_for(endpoint))}</loc></url>" for endpoint in PUBLIC_PAGES
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{locs}\n"
        "</urlset>\n"
    )


# ---------------------------------------------------------------------------
# Structured data (schema.org JSON-LD)
# ---------------------------------------------------------------------------

# The investigators, as structured data. This restates what /about already
# says in prose, which is a duplication worth paying for: it is how a search
# engine or an answer engine links the project to named researchers, their
# institutions and (via ORCID) the scholarly record, none of which it can
# reliably infer from a team-card grid. tests/unit/test_seo.py asserts every
# name here appears on the About page and vice versa, so the two cannot drift.
TEAM = (
    ("Patrik Wikstrom", "Project Leader", "Queensland University of Technology",
     "https://orcid.org/0000-0003-4720-0416"),
    ("Jean Burgess", "Chief Investigator", "Queensland University of Technology", ""),
    ("Jonathon Hutchinson", "Chief Investigator", "University of Sydney", ""),
    ("Joanne Gray", "Chief Investigator", "University of Sydney", ""),
    ("Ariadna Matamoros-Fernández", "Partner Investigator", "Dublin City University", ""),
    ("Jiaru Tang", "PhD Candidate", "Queensland University of Technology", ""),
    ("Tian Wen", "PhD Candidate", "University of Sydney", ""),
    ("Michelle Gay Nidoy", "PhD Candidate", "Queensland University of Technology", ""),
    ("Billie Wilcox", "Research Assistant", "University of Sydney", ""),
)

INSTITUTION_URLS = {
    "Queensland University of Technology": "https://www.qut.edu.au",
    "University of Sydney": "https://www.sydney.edu.au",
    "Dublin City University": "https://www.dcu.ie",
}

# Profiles that let a search engine reconcile this site with the same entity
# elsewhere. sameAs is the single highest-value property for that, and the
# project links both of these from /about already.
SAME_AS = (
    "https://www.linkedin.com/company/107376504/",
    "https://www.tiktok.com/@for.you.research",
)

FUNDER_NAME = "Australian Research Council"
FUNDER_URL = "https://www.arc.gov.au"
GRANT_ID = "DP240102939"


def _site_config():
    """``[site]`` as a mapping, or {} when config cannot be read."""
    try:
        from fyp.fyp_config import get_config

        return get_config().get("site", {}) or {}
    except Exception:
        return {}


def _organization_node():
    """The project itself: a ResearchProject with its people, funder and grant."""
    site = _site_config()
    node = {
        "@type": "ResearchProject",
        "@id": absolute("/#project"),
        "name": SITE_NAME,
        "alternateName": "For You Research",
        "url": absolute("/"),
        "description": PUBLIC_PAGES["public_bp.about"]["description"],
        "sameAs": list(SAME_AS),
        "parentOrganization": [
            {"@type": "CollegeOrUniversity", "name": name, "url": url}
            for name, url in (
                ("Queensland University of Technology", INSTITUTION_URLS[
                    "Queensland University of Technology"]),
                ("University of Sydney", INSTITUTION_URLS["University of Sydney"]),
            )
        ],
        "funder": {"@type": "Organization", "name": FUNDER_NAME, "url": FUNDER_URL},
        "funding": {
            "@type": "Grant",
            "identifier": GRANT_ID,
            "funder": {"@type": "Organization", "name": FUNDER_NAME, "url": FUNDER_URL},
        },
        "member": [_person_node(*member) for member in TEAM],
    }
    contact_email = str(site.get("contact_email", "") or "").strip()
    if contact_email:
        node["email"] = contact_email
    return node


def _person_node(name, role, institution, orcid):
    """One investigator, with affiliation and (where known) ORCID."""
    node = {
        "@type": "Person",
        "name": name,
        "jobTitle": role,
        "affiliation": {
            "@type": "CollegeOrUniversity",
            "name": institution,
            "url": INSTITUTION_URLS.get(institution, ""),
        },
    }
    if not node["affiliation"]["url"]:
        del node["affiliation"]["url"]
    if orcid:
        # ORCID as both identifier and sameAs: the first is the semantically
        # correct slot, the second is the one consumers actually follow.
        node["identifier"] = orcid
        node["sameAs"] = orcid
    return node


def _software_node():
    """The Hub as software, from CITATION.cff — the same source as the footer.

    No ``offers`` or ``aggregateRating``, which means Google will not draw a
    software rich result from this. Both would have to be invented, and
    fabricated ratings are a manual-action risk. What the node is for is entity
    resolution: tying the site to the repository, the DOI and the licence.

    ``codeRepository`` comes from ``[site] repo_url``, not from CITATION.cff.
    An operator who blanks ``repo_url`` has asked for no source-code links
    anywhere on the site, and structured data is not exempt from that just
    because a human cannot see it.
    """
    from web_interface.citation import get_citation

    citation = get_citation()
    if not citation.get("available"):
        return None

    repo_url = str(_site_config().get("repo_url", "") or "").strip().rstrip("/")

    node = {
        "@type": "SoftwareApplication",
        "@id": absolute("/thehub#software"),
        "name": DEFAULT_TITLE,
        "description": PUBLIC_PAGES["public_bp.thehub"]["description"],
        "url": absolute(url_for("public_bp.thehub")),
        "applicationCategory": "Research application",
        "operatingSystem": "Any (runs in a web browser)",
        "isAccessibleForFree": True,
        "license": "https://opensource.org/licenses/MIT",
        "author": {"@id": absolute("/#project")},
    }
    if repo_url:
        node["codeRepository"] = repo_url
    if citation.get("version"):
        node["softwareVersion"] = citation["version"]
    if citation.get("doi_url"):
        node["identifier"] = citation["doi_url"]
        node["sameAs"] = citation["doi_url"]
    return node


def json_ld(endpoint):
    """The JSON-LD ``@graph`` for ``endpoint``, as a string, or "" for none.

    One graph per page rather than scattered snippets: the WebSite and the
    project carry stable ``@id``s, so per-page nodes reference them instead of
    restating them, and a consumer sees one entity across the whole site.

    ``<`` is escaped so the payload cannot terminate the surrounding
    ``<script>`` element, whatever ends up in a description.
    """
    if endpoint not in PUBLIC_PAGES:
        return ""

    graph = [
        {
            "@type": "WebSite",
            "@id": absolute("/#website"),
            "url": absolute("/"),
            "name": SITE_NAME,
            "inLanguage": "en",
            "publisher": {"@id": absolute("/#project")},
        },
        _organization_node(),
    ]
    if endpoint == "public_bp.thehub":
        software = _software_node()
        if software:
            graph.append(software)

    payload = {"@context": "https://schema.org", "@graph": graph}
    return json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")


# ---------------------------------------------------------------------------
# What the templates render
# ---------------------------------------------------------------------------


def page_meta():
    """Every SEO value the current request's template needs.

    Exposed to templates as ``seo`` by a context processor, so a page sets
    nothing itself — adding a public page means adding it to PUBLIC_PAGES, and
    the title, description, canonical link, social card and sitemap entry all
    follow.

    Returns:
        A mapping. ``indexable`` is False for everything outside PUBLIC_PAGES
        (the app shell, login, signup), and the template then emits no
        canonical link, no social card and no structured data.
    """
    endpoint = request.endpoint or ""
    page = PUBLIC_PAGES.get(endpoint)
    if not page:
        return {"indexable": False, "title": DEFAULT_TITLE, "description": ""}

    return {
        "indexable": True,
        "title": page["title"],
        "description": page["description"],
        "canonical": absolute(url_for(endpoint)),
        "site_name": SITE_NAME,
        "image": absolute(url_for("static", filename="landing/og_card.jpg")),
        "json_ld": json_ld(endpoint),
    }
