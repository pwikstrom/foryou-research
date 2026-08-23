"""Collection ↔ user-account links and the AIO donor-data → account move.

Covers: the profile validator (age as number or bracket), passwordless
participant accounts (cannot log in, can be claimed), case-insensitive email
matching, placeholder numbering, fill-gaps-never-overwrite profile merging,
the link semantics (absent vs null vs set), unlink on user delete, orphan
placeholders, the cid-remap carry-over, and the one-off migration being
idempotent. Storage is an in-memory stand-in for the ``users`` and
``recoded`` locations — nothing touches disk.
"""

import copy
from unittest.mock import patch

import pandas as pd
import pytest

from web_interface import auth, collection_accounts as ca


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

class _Store:
    """Stateful stand-in for data_io over {location: {filename: obj}}."""

    def __init__(self):
        self.files: dict[str, dict] = {"users": {}, "recoded": {}, "archive": {}}

    def exists(self, storage_location="", filename="", **kw):
        return filename in self.files.get(storage_location, {})

    def listdir(self, storage_location="", return_absolute_path=False, **kw):
        return list(self.files.get(storage_location, {}).keys())

    def load_json(self, storage_location="", filename="", **kw):
        return copy.deepcopy(self.files.get(storage_location, {}).get(filename))

    def save_json(self, data=None, storage_location="", filename="", **kw):
        self.files.setdefault(storage_location, {})[filename] = copy.deepcopy(data)

    def remove(self, storage_location="", filename="", **kw):
        self.files.get(storage_location, {}).pop(filename, None)

    def load_parquet(self, storage_location="", filename="", **kw):
        return self.files.get(storage_location, {}).get(filename).copy()

    def save_parquet(self, df=None, storage_location="", filename="", **kw):
        self.files.setdefault(storage_location, {})[filename] = df.copy()


@pytest.fixture
def env():
    store = _Store()
    import web_interface.services.study_data as sd
    # auth, collection_accounts and study_data all import the same data_io
    # module object — patch each function ONCE (double-patching the same
    # attribute and unwinding in the wrong order would leak a mock into the
    # rest of the session).
    modules = {id(m): m for m in (auth.data_io, ca.data_io, sd.data_io)}.values()
    patches = []
    for mod in modules:
        for name in ("exists", "listdir", "load_json", "save_json", "remove", "load_parquet", "save_parquet"):
            patches.append(patch.object(mod, name, side_effect=getattr(store, name)))
    patches.append(patch.object(ca, "placeholder_domain", return_value="example.test"))
    for p in patches:
        p.start()
    sd.invalidate_collection_tags_cache()
    try:
        um = auth.UserManager(storage_location="users", bootstrap=False)
        um.add_user("admin@admin.net", "pw", "admin", approved=True)
        yield store, um
    finally:
        for p in reversed(patches):
            p.stop()
        sd.invalidate_collection_tags_cache()


def _tags(store):
    return store.files["recoded"].get("collections_tags.json", {})


# --------------------------------------------------------------------------
# Profile validation
# --------------------------------------------------------------------------

def test_validate_profile_accepts_age_number_and_bracket():
    cleaned, err = auth.validate_profile({"age": "34"})
    assert err is None and cleaned["age"] == "34"
    cleaned, err = auth.validate_profile({"age": "21 - 25"})
    assert err is None and cleaned["age"] == "21 - 25"
    cleaned, err = auth.validate_profile({"age": "21-25"})
    assert cleaned["age"] == "21 - 25"
    cleaned, err = auth.validate_profile({"age": 42})
    assert cleaned["age"] == "42"


def test_validate_profile_rejects_bad_values_and_unknown_keys():
    assert auth.validate_profile({"age": "twenty"})[1]
    assert auth.validate_profile({"age": "130"})[1]
    assert auth.validate_profile({"favourite_colour": "blue"})[1]
    assert auth.validate_profile({"full_name": "x" * 101})[1]
    assert auth.validate_profile({"consent_to_contact": "maybe"})[1]


def test_validate_profile_clears_empty_and_maps_consent():
    cleaned, err = auth.validate_profile({"postcode": "  ", "consent_to_contact": "yes", "country": " AU "})
    assert err is None
    assert cleaned == {"postcode": None, "consent_to_contact": True, "country": "AU"}


def test_sanitize_profile_drops_only_bad_fields():
    cleaned, dropped = auth.sanitize_profile({"age": "n/a", "country": "AU", "bogus": 1})
    assert cleaned == {"country": "AU"}
    assert set(dropped) == {"age", "bogus"}


# --------------------------------------------------------------------------
# Passwordless participant accounts
# --------------------------------------------------------------------------

def test_participant_account_cannot_login_until_claimed(env):
    store, um = env
    ok, _ = um.add_user("p@x.org", None, "viewer", approved=True,
                        account_kind=auth.ACCOUNT_KIND_PARTICIPANT, profile={"age": "30"})
    assert ok
    u = um.get_user("p@x.org")
    assert not u.can_login()
    assert um.verify_user("p@x.org", "") is None
    assert auth.verify_password(None, "anything") is False
    assert store.files["users"]["p@x.org.json"]["profile"]["age"] == "30"

    ok, msg = um.claim_participant_account("p@x.org", "secret", "Pat")
    assert ok, msg
    assert um.verify_user("p@x.org", "secret") is not None
    assert um.get_user("p@x.org").profile["age"] == "30"  # profile survives the claim
    # A claimed (login-capable) account is not claimable again.
    assert not um.claim_participant_account("p@x.org", "other")[0]


def test_placeholder_cannot_be_claimed(env):
    store, um = env
    um.add_user("p-1@example.test", None, "viewer", approved=True,
                account_kind=auth.ACCOUNT_KIND_PARTICIPANT, placeholder=True)
    assert not um.claim_participant_account("p-1@example.test", "pw")[0]


def test_find_user_by_email_is_case_insensitive_and_placeholder_numbering(env):
    store, um = env
    um.add_user("Mixed.Case@Example.COM", "pw", "viewer")
    assert um.find_user_by_email("mixed.case@example.com").username == "Mixed.Case@Example.COM"
    assert um.find_user_by_email("nobody@example.com") is None

    assert um.next_placeholder_username("example.test") == "p-1@example.test"
    um.add_user("p-7@example.test", None, "viewer", placeholder=True)
    assert um.next_placeholder_username("example.test") == "p-8@example.test"
    # A number still referenced by a collection link is not reissued even
    # after the account itself is gone (the link would point at a new person).
    um.add_user("p-8@example.test", None, "viewer", placeholder=True)
    ca.set_collection_owner("c_stale", "p-8@example.test")
    um.delete_user("p-8@example.test")
    assert um.next_placeholder_username("example.test") == "p-8@example.test"
    assert ca.next_placeholder_username(um=um) == "p-9@example.test"


def test_fill_profile_gaps_never_overwrites(env):
    store, um = env
    um.add_user("a@x.org", "pw", "viewer", profile={"full_name": "Set By Person"})
    filled, conflicts = um.fill_profile_gaps("a@x.org", {"full_name": "Donation Name", "age": "34", "postcode": ""})
    assert filled == {"age": "34"}
    assert conflicts == {"full_name": {"kept": "Set By Person", "offered": "Donation Name"}}
    assert um.get_user("a@x.org").profile["full_name"] == "Set By Person"


def test_delete_user_removes_log_sidecar(env):
    store, um = env
    um.add_user("z@x.org", "pw", "viewer")
    store.files["users"]["z@x.org_log.json"] = [{"a": 1}]
    assert um.delete_user("z@x.org")[0]
    assert "z@x.org.json" not in store.files["users"]
    assert "z@x.org_log.json" not in store.files["users"]


# --------------------------------------------------------------------------
# Link semantics
# --------------------------------------------------------------------------

def test_set_owner_preserves_other_keys_and_unlink(env):
    store, um = env
    store.files["recoded"]["collections_tags.json"] = {
        "c1": {"display_collection_id": "One", "annotation_tags": ["t"], "hidden": False},
    }
    ca.set_collection_owner("c1", "admin@admin.net")
    ca.set_collection_owner("c2", "admin@admin.net")
    t = _tags(store)
    assert t["c1"]["user_id"] == "admin@admin.net" and t["c1"]["annotation_tags"] == ["t"]
    assert t["c2"]["user_id"] == "admin@admin.net"
    assert ca.collections_for_user("admin@admin.net", fresh=True) == ["c1", "c2"]
    assert ca.collection_counts_by_user(fresh=True) == {"admin@admin.net": 2}

    assert ca.unlink_user("admin@admin.net") == ["c1", "c2"]
    t = _tags(store)
    assert t["c1"]["user_id"] is None and "user_id" in t["c2"]
    assert ca.collections_for_user("admin@admin.net", fresh=True) == []


def test_link_aio_respects_decided_links_and_creates_accounts(env):
    store, um = env
    um.add_user("Known@Example.org", "pw", "viewer")
    store.files["recoded"]["collections_tags.json"] = {
        "c_unassigned": {"annotation_tags": [], "user_id": None},   # admin said: no account
        "c_linked": {"annotation_tags": [], "user_id": "admin@admin.net"},
    }
    raw = {
        "c_known": {"id": "c_known", "email": "known@example.org", "age": ["34"], "profile": "x", "pk": "y"},
        "c_new": {"id": "c_new", "email": "New.Person@Example.org", "name": "New Person", "postCode": "4000"},
        "c_noemail": {"id": "c_noemail", "age": ["21 - 25"], "country": "Australia"},
        "c_consent_only": {"id": "c_consent_only", "consentToContact": True},
        "c_unassigned": {"id": "c_unassigned", "email": "known@example.org"},
        "c_linked": {"id": "c_linked", "email": "known@example.org"},
        "c_not_in_dataset": {"id": "c_not_in_dataset", "email": "other@example.org"},
    }
    report = ca.link_aio_collections(raw, restrict_to=set(raw) - {"c_not_in_dataset"}, um=um)

    assert report["outcomes"]["c_known"] == "existing"
    assert report["outcomes"]["c_new"] == "created"
    assert report["outcomes"]["c_noemail"] == "placeholder"
    assert report["outcomes"]["c_consent_only"] == "skipped"
    assert report["outcomes"]["c_unassigned"] == "already_decided"
    assert report["outcomes"]["c_linked"] == "already_decided"
    assert "c_not_in_dataset" not in report["outcomes"]

    t = _tags(store)
    assert t["c_known"]["user_id"] == "Known@Example.org"
    assert t["c_new"]["user_id"] == "new.person@example.org"
    assert t["c_noemail"]["user_id"] == "p-1@example.test"
    assert t["c_unassigned"]["user_id"] is None
    assert "c_consent_only" not in t

    known = um.get_user("Known@Example.org")
    assert known.profile["age"] == "34"
    new = um.get_user("new.person@example.org")
    assert new.account_kind == "participant" and not new.can_login() and not new.placeholder
    assert new.profile["full_name"] == "New Person" and new.profile["postcode"] == "4000"
    assert new.origin["source"] == "aio_ingest" and new.origin["collection_id"] == "c_new"
    ph = um.get_user("p-1@example.test")
    assert ph.placeholder and ph.profile["age"] == "21 - 25" and ph.profile["country"] == "Australia"

    # Second run is a no-op: everything is decided now.
    again = ca.link_aio_collections(raw, restrict_to=set(raw) - {"c_not_in_dataset"}, um=um)
    assert again["linked"] == {}
    assert not again["created_accounts"] and not again["placeholders"]


def test_link_aio_dry_run_writes_nothing_but_reports(env):
    store, um = env
    raw = {
        "c1": {"id": "c1", "email": "a@b.org", "name": "A"},
        "c2": {"id": "c2", "email": "a@b.org"},
        "c3": {"id": "c3", "age": ["40"]},
        "c4": {"id": "c4", "age": ["41"]},
    }
    report = ca.link_aio_collections(raw, dry_run=True, um=um)
    assert report["outcomes"] == {"c1": "created", "c2": "existing", "c3": "placeholder", "c4": "placeholder"}
    assert report["placeholders"] == ["p-1@example.test", "p-2@example.test"]
    assert "collections_tags.json" not in store.files["recoded"]
    assert um.get_user("a@b.org") is None


def test_orphan_placeholders(env):
    store, um = env
    um.add_user("p-1@example.test", None, "viewer", placeholder=True)
    um.add_user("p-2@example.test", None, "viewer", placeholder=True)
    um.add_user("member@x.org", "pw", "viewer")
    ca.set_collection_owner("c1", "p-1@example.test")
    assert ca.orphan_placeholder_accounts(um=um) == ["p-2@example.test"]


# --------------------------------------------------------------------------
# cid remap carries the link
# --------------------------------------------------------------------------

def test_cid_remap_carries_user_id():
    from fyp.ingest import base as ingest_base

    tags = {
        "old1": {"annotation_tags": ["a"], "user_id": "u1"},
        "new1": {"annotation_tags": ["b"]},
        "old2": {"annotation_tags": [], "user_id": "u2"},
    }
    files = {"collections_tags.json": tags}
    with patch.object(ingest_base.data_io, "exists", side_effect=lambda storage_location, filename: filename in files), \
         patch.object(ingest_base.data_io, "load_json", side_effect=lambda storage_location, filename, **kw: copy.deepcopy(files[filename])), \
         patch.object(ingest_base.data_io, "save_json", side_effect=lambda data, storage_location, filename, **kw: files.__setitem__(filename, data)):
        ingest_base.apply_cid_remap_to_metadata({"old1": "new1", "old2": "new2"})
    assert files["collections_tags.json"]["new1"]["user_id"] == "u1"
    assert files["collections_tags.json"]["new2"]["user_id"] == "u2"


# --------------------------------------------------------------------------
# Metadata writers never emit demographic columns
# --------------------------------------------------------------------------

def test_strip_demographic_columns_handles_tuple_and_string_labels():
    from fyp.donations import strip_demographic_columns

    df = pd.DataFrame({("counts", "total"): [1], ("participants", "email"): ["a@b"],
                       ("participants", "campaign"): ["qut"], ("participants", "age"): [["34"]]})
    out = strip_demographic_columns(df)
    assert list(out.columns) == [("counts", "total"), ("participants", "campaign")]

    df2 = pd.DataFrame({"('participants', 'email')": ["a@b"], "('participants', 'campaign')": ["qut"]})
    assert list(strip_demographic_columns(df2).columns) == ["('participants', 'campaign')"]


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def _metadata_frame():
    cols = pd.MultiIndex.from_tuples([
        ("counts", "total"), ("participants", "campaign"), ("participants", "email"),
        ("participants", "name"), ("participants", "age"), ("participants", "postCode"),
        ("participants", "consentToContact"),
    ])
    df = pd.DataFrame([
        [10, "qut", "Known@Example.org", "Known Person", ["34"], None, True],
        [20, "qut", None, "Anon", ["21 - 25"], "4000", None],
        [30, "qut", None, None, None, None, False],
        [40, "qut", "dup@example.org", "Dup", ["30"], None, None],
        [50, "qut", "dup@example.org", "Dup", ["30"], None, None],
    ], columns=cols, index=pd.Index(["c1", "c2", "c3", "c4", "c5"], name="collection_id"))
    return df


def test_migration_dry_run_then_apply_then_noop(env):
    store, um = env
    um.add_user("known@example.org", "pw", "viewer")
    store.files["recoded"]["collections_metadata.parquet"] = _metadata_frame()
    store.files["recoded"]["collections_tags.json"] = {"c1": {"annotation_tags": ["keep"]}}

    dry = ca.migrate_existing_collections(dry_run=True, um=um, log=lambda *_: None)
    assert dry["dry_run"] and len(dry["linked"]) == 4
    assert "collections_tags.json" in store.files["recoded"]
    assert _tags(store) == {"c1": {"annotation_tags": ["keep"]}}  # untouched
    assert store.files["archive"] == {}

    applied = ca.migrate_existing_collections(dry_run=False, um=um, log=lambda *_: None)
    assert applied["outcomes"] == {"c1": "existing", "c2": "placeholder", "c4": "created", "c5": "existing"}
    t = _tags(store)
    assert t["c1"]["user_id"] == "known@example.org" and t["c1"]["annotation_tags"] == ["keep"]
    assert t["c2"]["user_id"] == "p-1@example.test"
    assert t["c4"]["user_id"] == "dup@example.org" == t["c5"]["user_id"]
    assert "c3" not in t  # consent flag alone earns no account
    assert um.get_user("known@example.org").profile["age"] == "34"
    md = store.files["recoded"]["collections_metadata.parquet"]
    assert [c for c in md.columns if c[0] == "participants"] == [("participants", "campaign")]
    assert any(f.startswith("collections_metadata_pre_accounts_") for f in store.files["archive"])
    assert any(f.startswith("collection_accounts_migration_") for f in store.files["recoded"])

    n_users = len(store.files["users"])
    again = ca.migrate_existing_collections(dry_run=False, um=um, log=lambda *_: None)
    assert again["linked"] == {} and len(store.files["users"]) == n_users
