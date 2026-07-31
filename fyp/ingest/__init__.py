"""Data-donation ingestion: base collection classes + per-platform parsers.

``fyp/ingest.py`` became this package; the ``__init__`` re-exports the old
module's surface so every ``from fyp.ingest import X`` is unchanged.

Unlike every other fyp subpackage this ``__init__`` is EAGER by design:
importing ``fyp.ingest`` has always triggered the config boot, because the
platform collection subclasses self-register their raw-upload locations at
class definition (``__init_subclass__`` -> ``data_io.register_location()``)
and the upload routes depend on that. The platform modules are imported in a
pinned order so the registry (and ``registered_raw_locations()``) order is
byte-identical to the flat module.
"""

from fyp.ingest import base as _base
from fyp.ingest.base import (
    COLLECTION_TAGS_FILENAME,
    INGESTION_LEDGER_FILENAME,
    LEDGER_SKIP_OUTCOMES,
    LEGACY_DISCARDED_FILENAME,
    STUDIES_FILENAME,
    WEEKDAY_MAPPER,
    ForYouBaseCollection,
    ForYouCollection,
    apply_cid_remap_to_metadata,
    assign_session_ids,
    derive_play_duration,
    get_main_collection,
    parse_donor_timezone,
    registered_raw_locations,
)
# Platform modules are imported in pinned order (tiktok -> instagram ->
# youtube) so __init_subclass__ registration order stays byte-identical to
# the flat module (guarded by tests/unit/test_subpackage_shims.py).
from fyp.ingest.tiktok import (
    TikTokAIOCollection,
    TikTokDDPCollection,
    TikTokDemoCollection,
    TikTokZeeschuimerCollection,
)
from fyp.ingest.instagram import InstagramDDPCollection
from fyp.ingest.youtube import YouTubeDDPCollection





def __getattr__(name: str):
    """Forward stragglers (incl. private helpers) to the base module."""
    return getattr(_base, name)
