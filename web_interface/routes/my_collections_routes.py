"""API routes for the "My Collections" My-stuff page.

Access is by ownership (``user_id`` links in collections_tags.json), not study
membership — see :func:`._access.owned_collection_access_error`. Everything
served here is computed from donated activity data only (no scrape, no
annotation): the participant sees their own data the moment it is ingested.
"""

from flask import Blueprint, jsonify
from flask_login import current_user

from ..permissions import permission_required
from ._access import owned_collection_access_error

my_collections_bp = Blueprint('my_collections_bp', __name__)


@my_collections_bp.route('/api/my/collections')
@permission_required('tab.my_stuff.my_collections')
def api_my_collections():
    """List the current user's own collections with light picker metadata."""
    from ..services.my_collections_service import list_owned_collections
    return jsonify({"collections": list_owned_collections(current_user.username)})


@my_collections_bp.route('/api/my/collections/combined/personality')
@permission_required('tab.my_stuff.my_collections')
def api_my_combined_personality():
    """The cross-platform personality bundle over ALL the user's collections.

    Registered before the ``<collection_id>`` route matters not at all to
    Flask's routing (static segments win over converters), but keep the name
    ``combined`` reserved — a collection id can never claim it.
    """
    from ..collection_accounts import collections_for_user
    from ..services.my_collections_service import build_personality
    cids = collections_for_user(current_user.username)
    if not cids:
        return jsonify({"error": "No collections are linked to your account"}), 404
    bundle = build_personality(cids)
    if bundle is None:
        return jsonify({"error": "No donated activity data found for your collections"}), 404
    return jsonify(bundle)


@my_collections_bp.route('/api/my/collections/<collection_id>/personality')
@permission_required('tab.my_stuff.my_collections', 'tab.data_management.edit_collections')
def api_my_collection_personality(collection_id):
    """The personality bundle for one of the user's own collections.

    Also serves the Edit Collections modal (OR-gated on the pipeline
    permission), where the ownership check is waived — see
    ``owned_collection_access_error``.
    """
    err = owned_collection_access_error(collection_id)
    if err:
        return err
    from ..services.my_collections_service import build_personality
    bundle = build_personality([collection_id])
    if bundle is None:
        return jsonify({"error": "No donated activity data found for this collection"}), 404
    return jsonify(bundle)
