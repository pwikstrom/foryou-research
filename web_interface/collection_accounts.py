"""Collection ↔ user-account links, and moving AIO donor data onto accounts.

A collection belongs to at most one user account (n-to-1). The link is the
``user_id`` key of the collection's entry in ``recoded/collections_tags.json``
— the per-collection sidecar that already carries display id, tags and the
hidden flag, is pruned by the delete worker and remapped on collection-id
merges. Three states matter:

* key **absent** — never decided; an AIO ingest may auto-link it;
* ``null`` — an admin explicitly unassigned it; ingest must NOT re-link;
* a username — linked.

The user side is derived by scanning that file (a few hundred entries), so
there is exactly one source of truth.

Demographic/contact data a participant submitted with a donation (email, name,
age, country, postcode, TikTok handle, consent to contact) never lives on the
collection: :func:`link_aio_collections` resolves or creates the participant's
account and fills the profile, and the metadata writers strip those columns
(:func:`fyp.donations.strip_demographic_columns`). The same function serves
the ingest worker and the one-off migration.

The user manager is injectable (``um=``) so the module is unit-testable
without the Flask security singleton.
"""

import ast
import datetime as _dt
import logging
import re

import pandas as pd

import fyp.data_io as data_io
from fyp.donations import demographic_metadata_columns
from fyp.fyp_config import fyp_cf
from fyp.organize_datasets import COLLECTIONS_LABEL

from .services.study_data import get_collection_tags, invalidate_collection_tags_cache

logger = logging.getLogger(__name__)

ORIGIN_AIO_INGEST = "aio_ingest"
ORIGIN_AIO_MIGRATION = "aio_migration"

# AIO attribute → profile field. ``email`` is the account id, not a profile field.
_AIO_TO_PROFILE = {
    "name": "full_name",
    "age": "age",
    "country": "country",
    "postCode": "postcode",
    "tiktokHandle": "tiktok_handle",
    "consentToContact": "consent_to_contact",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# A record is worth an account only if it says something about WHO the
# participant is. A lone consent flag is not — without an email or any of
# these, no account is created and the collection stays unlinked.
_IDENTITY_PROFILE_FIELDS = ("full_name", "age", "country", "postcode", "tiktok_handle")


def _um(um=None):
    if um is not None:
        return um
    # Function-level import: the security module builds the Flask login
    # manager, which the ingest worker must not pull in at import time.
    from .security import user_manager
    return user_manager


def _tags_filename() -> str:
    return f"{COLLECTIONS_LABEL}_tags.json"


def _load_tags_fresh() -> dict:
    """Read collections_tags.json from storage, bypassing the RAM cache.

    Writers must read fresh: the cache can be up to five minutes stale and a
    read-modify-write on it would resurrect entries another process changed.
    """
    fn = _tags_filename()
    if data_io.exists(storage_location="recoded", filename=fn):
        return data_io.load_json(storage_location="recoded", filename=fn, verbose=False) or {}
    return {}


def _save_tags(tags: dict) -> None:
    data_io.save_json(data=tags, storage_location="recoded", filename=_tags_filename(), verbose=False)
    invalidate_collection_tags_cache()


def placeholder_domain() -> str:
    return (fyp_cf.get("site") or {}).get("participant_placeholder_domain") or "foryouresearch.net"


def next_placeholder_username(um=None, tags: dict | None = None) -> str:
    """Next free ``p-N@<domain>``: one past the highest N seen in the roster
    OR still referenced by any collection link — so a deleted placeholder's
    number is never reissued while a link still carries it."""
    um = _um(um)
    domain = placeholder_domain()
    candidate = um.next_placeholder_username(domain)
    pattern = re.compile(rf"^p-(\d+)@{re.escape(domain)}$", re.IGNORECASE)
    highest = int(pattern.match(candidate).group(1)) - 1
    entries = tags if tags is not None else _load_tags_fresh()
    for entry in entries.values():
        uid = entry.get("user_id") if isinstance(entry, dict) else None
        m = pattern.match(uid) if isinstance(uid, str) else None
        if m:
            highest = max(highest, int(m.group(1)))
    return f"p-{highest + 1}@{domain}"


# ---------------------------------------------------------------------------
# Link reads
# ---------------------------------------------------------------------------

def load_owner_map(fresh: bool = False) -> dict:
    """Return ``{collection_id: user_id}`` for every collection with a decided
    link (``user_id`` may be None for explicitly unassigned collections)."""
    tags = _load_tags_fresh() if fresh else get_collection_tags()
    return {cid: entry.get("user_id") for cid, entry in tags.items()
            if isinstance(entry, dict) and "user_id" in entry}


def collections_for_user(user_id: str, fresh: bool = False) -> list[str]:
    """Collection ids linked to ``user_id`` (exact match on the account id)."""
    return sorted(cid for cid, uid in load_owner_map(fresh=fresh).items() if uid == user_id)


def collection_counts_by_user(fresh: bool = False) -> dict:
    counts: dict = {}
    for uid in load_owner_map(fresh=fresh).values():
        if uid:
            counts[uid] = counts.get(uid, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Link writes
# ---------------------------------------------------------------------------

def set_collection_owner(collection_id: str, user_id, *, tags: dict | None = None) -> dict:
    """Link ``collection_id`` to ``user_id`` (None = explicitly unassigned).

    Preserves every other key of the sidecar entry. Pass ``tags`` to batch
    several writes: the caller then owns the load and the final save.
    """
    own = tags is None
    if own:
        tags = _load_tags_fresh()
    entry = tags.get(str(collection_id))
    if not isinstance(entry, dict):
        entry = {"display_collection_id": None, "annotation_tags": [], "hidden": False}
    entry["user_id"] = user_id
    tags[str(collection_id)] = entry
    if own:
        _save_tags(tags)
    return tags


def drop_collection_entry(collection_id: str) -> bool:
    """Delete a collection's sidecar entry outright.

    Used when a pending self-serve upload is rejected before ingestion — the
    entry was created at upload time and nothing else references it. Never
    call this for a collection that is already in the dataset.
    """
    tags = _load_tags_fresh()
    if str(collection_id) in tags:
        del tags[str(collection_id)]
        _save_tags(tags)
        return True
    return False


def unlink_user(user_id: str) -> list[str]:
    """Set every collection linked to ``user_id`` to unassigned (``null``).

    Returns the affected collection ids. Used before an account is deleted so
    no link ever points at a username that no longer exists.
    """
    tags = _load_tags_fresh()
    affected = []
    for cid, entry in tags.items():
        if isinstance(entry, dict) and entry.get("user_id") == user_id:
            entry["user_id"] = None
            affected.append(cid)
    if affected:
        _save_tags(tags)
    return sorted(affected)


def orphan_placeholder_accounts(um=None) -> list[str]:
    """Placeholder (p-N) accounts that own no collection.

    A placeholder holds only demographics that arrived with a donation, so
    once its collections are gone it has no reason to exist — but removal is
    an explicit admin action, never automatic.
    """
    um = _um(um)
    counts = collection_counts_by_user(fresh=True)
    return sorted(u.username for u in um.get_all_users().values()
                  if u.placeholder and counts.get(u.username, 0) == 0)


# ---------------------------------------------------------------------------
# AIO donor data → accounts
# ---------------------------------------------------------------------------

def _clean_scalar(value):
    """Normalise a DynamoDB-deserialised (or parquet-round-tripped) value:
    unwrap one-element lists/arrays, strip strings, map empty/NA/None to None."""
    if value is None:
        return None
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        value = value.tolist()  # numpy array / pyarrow scalar → python
    if isinstance(value, (list, tuple)):
        return _clean_scalar(value[0]) if value else None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    text = str(value).strip()
    if not text or text.lower() in ("none", "nan", "null", "<na>"):
        return None
    return text


def participant_from_aio(item: dict) -> dict:
    """Reduce a raw AIO item to ``{"email": …, "profile": {...}}`` (cleaned).

    Returns an empty dict when the item carries no demographic value at all.
    """
    email = _clean_scalar(item.get("email"))
    if isinstance(email, str):
        email = email.lower()
        if not _EMAIL_RE.match(email):
            email = None
    else:
        email = None
    profile: dict = {}
    for aio_key, prof_key in _AIO_TO_PROFILE.items():
        val = _clean_scalar(item.get(aio_key))
        if val is not None:
            profile[prof_key] = val
    if email is None and not any(profile.get(k) is not None for k in _IDENTITY_PROFILE_FIELDS):
        return {}
    return {"email": email, "profile": profile}


def participants_from_metadata_frame(df) -> dict:
    """Build ``{collection_id: participant}`` from a collections metadata frame
    that still carries ``('participants', <demographic>)`` columns — the
    migration's input. Handles both tuple and stringified-tuple labels."""
    cols = demographic_metadata_columns(df.columns)
    if not cols:
        return {}
    out: dict = {}
    field_of = {c: (c[1] if isinstance(c, tuple) else ast.literal_eval(c)[1]) for c in cols}
    for cid, row in df[cols].iterrows():
        item = {field_of[c]: row[c] for c in cols}
        participant = participant_from_aio(item)
        if participant:
            out[str(cid)] = participant
    return out


def resolve_or_create_account(participant: dict, *, origin_source: str, collection_id: str,
                              dry_run: bool = False, um=None, tags: dict | None = None) -> tuple[str | None, str, dict]:
    """Find or create the account for one participant record.

    Returns ``(user_id, outcome, details)`` where outcome is one of
    ``existing`` (email matched a user), ``created`` (new participant account
    under the real email), ``placeholder`` (no email → p-N account) or
    ``skipped`` (nothing usable). In ``dry_run`` nothing is written and the
    would-be username is returned (placeholders are numbered provisionally).
    """
    um = _um(um)
    email = participant.get("email")
    profile = dict(participant.get("profile") or {})
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    origin = {"source": origin_source, "at": now, "collection_id": str(collection_id)}
    details: dict = {}

    if email:
        existing = um.find_user_by_email(email)
        if existing is not None:
            if not dry_run and profile:
                filled, conflicts = um.fill_profile_gaps(existing.username, profile)
                details = {"filled": filled, "conflicts": conflicts}
            return existing.username, "existing", details
        if dry_run:
            return email, "created", {"profile": profile}
        from .admin_settings import get_default_new_user_role
        from .auth import ACCOUNT_KIND_PARTICIPANT
        ok, msg = um.add_user(email, None, get_default_new_user_role(), approved=True,
                              account_kind=ACCOUNT_KIND_PARTICIPANT, profile=profile, origin=origin)
        if not ok:
            return None, "skipped", {"error": msg}
        return email, "created", {"profile": profile}

    if not any(profile.get(k) is not None for k in _IDENTITY_PROFILE_FIELDS):
        return None, "skipped", {"error": "no email and no identifying demographic data"}

    username = next_placeholder_username(um=um, tags=tags)
    if dry_run:
        return username, "placeholder", {"profile": profile}
    from .admin_settings import get_default_new_user_role
    from .auth import ACCOUNT_KIND_PARTICIPANT
    ok, msg = um.add_user(username, None, get_default_new_user_role(), approved=True,
                          account_kind=ACCOUNT_KIND_PARTICIPANT, profile=profile, origin=origin,
                          placeholder=True)
    if not ok:
        return None, "skipped", {"error": msg}
    return username, "placeholder", {"profile": profile}


def link_aio_collections(participant_metadata: dict, *, origin_source: str = ORIGIN_AIO_INGEST,
                         dry_run: bool = False, only_undecided: bool = True,
                         restrict_to: set | None = None, um=None) -> dict:
    """Link collections to accounts from AIO donor data.

    ``participant_metadata`` is ``{collection_id: raw_aio_item}`` (as from
    :func:`fyp.donations.load_aio_participant_metadata`) or
    ``{collection_id: {"email", "profile"}}`` (already reduced). Collections
    whose link is already decided are skipped when ``only_undecided`` is set
    (the default — an admin's explicit unassignment is never overridden).
    ``restrict_to`` limits the run to those collection ids.

    Returns a report dict; in ``dry_run`` mode nothing is written. Placeholder
    numbering in a dry run is provisional ("p-N+1" for every new one) since
    no account is created to claim the number.
    """
    um = _um(um)
    tags = _load_tags_fresh()
    decided = {cid for cid, entry in tags.items() if isinstance(entry, dict) and "user_id" in entry}

    report: dict = {
        "dry_run": dry_run,
        "linked": {},            # cid -> user_id
        "outcomes": {},          # cid -> existing|created|placeholder|skipped|already_decided
        "created_accounts": [],  # usernames created under a real email
        "placeholders": [],      # placeholder usernames created
        "conflicts": {},         # user_id -> {field: {kept, offered}}
        "skipped": {},           # cid -> reason
    }

    changed = False
    for cid, raw in participant_metadata.items():
        cid = str(cid)
        if restrict_to is not None and cid not in restrict_to:
            continue
        if only_undecided and cid in decided:
            report["outcomes"][cid] = "already_decided"
            continue
        # A raw AIO item also has a "profile" attribute (table plumbing), so
        # "already reduced" means exactly the two reduced keys and nothing else.
        participant = raw if set(raw.keys()) == {"email", "profile"} else participant_from_aio(raw)
        if not participant:
            report["outcomes"][cid] = "skipped"
            report["skipped"][cid] = "no demographic data"
            continue
        user_id, outcome, details = resolve_or_create_account(
            participant, origin_source=origin_source, collection_id=cid, dry_run=dry_run, um=um, tags=tags)
        if dry_run and outcome == "created":
            # Same email on a later collection: the real run would find the
            # account it just created, so report it as existing.
            if user_id in report["created_accounts"]:
                outcome = "existing"
        report["outcomes"][cid] = outcome
        if outcome == "skipped" or not user_id:
            report["skipped"][cid] = details.get("error", "unresolved")
            continue
        if outcome == "created":
            report["created_accounts"].append(user_id)
        elif outcome == "placeholder":
            if dry_run:
                # Nothing was created to claim the number, so count forward
                # ourselves to show the names the real run would mint.
                n_prev = len(report["placeholders"])
                m = re.match(r"^p-(\d+)@(.+)$", user_id)
                if m:
                    user_id = f"p-{int(m.group(1)) + n_prev}@{m.group(2)}"
            report["placeholders"].append(user_id)
        if details.get("conflicts"):
            report["conflicts"].setdefault(user_id, {}).update(details["conflicts"])
        report["linked"][cid] = user_id
        if not dry_run:
            set_collection_owner(cid, user_id, tags=tags)
            changed = True

    if changed:
        _save_tags(tags)
    return report


# ---------------------------------------------------------------------------
# One-off migration of existing collections
# ---------------------------------------------------------------------------

def migrate_existing_collections(*, dry_run: bool = True, um=None, log=print) -> dict:
    """Move demographic data off every existing collection onto user accounts.

    Idempotent: links already decided are left alone and a parquet without
    demographic columns is left alone, so a second run is a no-op. With
    ``dry_run`` (the default) nothing is written — the report shows what an
    apply would do. An apply first snapshots the metadata parquet and the tags
    sidecar into the ``archive`` location (timestamped), then links, then
    strips the columns and saves, then writes a JSON report next to the parquet.
    """
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    meta_fn = f"{COLLECTIONS_LABEL}_metadata.parquet"
    tags_fn = _tags_filename()

    if not data_io.exists(storage_location="recoded", filename=meta_fn):
        log(f"No {meta_fn} in 'recoded' — nothing to migrate.")
        return {"dry_run": dry_run, "linked": {}, "columns_stripped": [], "outcomes": {}}

    df = data_io.load_parquet(storage_location="recoded", filename=meta_fn, verbose=False)
    if "collection_id" in df.columns:
        df = df.set_index("collection_id")
    demographic_cols = demographic_metadata_columns(df.columns)
    participants = participants_from_metadata_frame(df) if demographic_cols else {}
    log(f"Loaded {len(df):,} collections; {len(demographic_cols)} demographic column(s); "
        f"{len(participants)} collection(s) carry demographic data.")

    if not demographic_cols:
        log("No demographic columns in the parquet — already migrated; nothing to do.")
        return {"dry_run": dry_run, "linked": {}, "columns_stripped": [], "outcomes": {},
                "created_accounts": [], "placeholders": [], "conflicts": {}, "skipped": {}}

    if not dry_run:
        # Snapshot both artefacts before touching anything. A copy (not a
        # move): the live files stay in place for the rest of the run.
        data_io.save_parquet(df=df, storage_location="archive",
                             filename=f"{COLLECTIONS_LABEL}_metadata_pre_accounts_{ts}.parquet", verbose=False)
        data_io.save_json(data=_load_tags_fresh(), storage_location="archive",
                          filename=f"{COLLECTIONS_LABEL}_tags_pre_accounts_{ts}.json", verbose=False)
        log(f"Snapshots written to 'archive' (suffix {ts}).")

    report = link_aio_collections(participants, origin_source=ORIGIN_AIO_MIGRATION,
                                  dry_run=dry_run, only_undecided=True, um=um)
    report["columns_stripped"] = [list(c) if isinstance(c, tuple) else c for c in demographic_cols]
    report["timestamp"] = ts
    log(summarize_report(report))

    if dry_run:
        log(f"Would strip {len(demographic_cols)} column(s): {report['columns_stripped']}")
    else:
        stripped = df.drop(columns=demographic_cols)
        data_io.save_parquet(df=stripped, storage_location="recoded", filename=meta_fn, verbose=False)
        log(f"Stripped {len(demographic_cols)} demographic column(s) from {meta_fn}.")

    if not dry_run:
        data_io.save_json(data=report, storage_location="recoded",
                          filename=f"collection_accounts_migration_{ts}.json", verbose=False)
        log(f"Report saved: recoded/collection_accounts_migration_{ts}.json")
    return report


def summarize_report(report: dict) -> str:
    n_link = len(report.get("linked", {}))
    n_created = len(report.get("created_accounts", []))
    n_ph = len(report.get("placeholders", []))
    n_existing = sum(1 for v in report.get("outcomes", {}).values() if v == "existing")
    n_decided = sum(1 for v in report.get("outcomes", {}).values() if v == "already_decided")
    n_skip = len(report.get("skipped", {}))
    n_conf = sum(len(v) for v in report.get("conflicts", {}).values())
    mode = "DRY RUN — " if report.get("dry_run") else ""
    return (f"{mode}{n_link} collection(s) linked: {n_existing} to existing accounts, "
            f"{n_created} new participant accounts, {n_ph} placeholders; "
            f"{n_decided} already decided, {n_skip} skipped, {n_conf} profile conflict(s) kept existing values.")
