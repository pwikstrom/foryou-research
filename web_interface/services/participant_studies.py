"""Auto-managed participant studies ("Just Me" / "Everyone & Me").

Every user who owns donated collections gets a fixed pair of system studies:

* ``__me__{username}`` — **Just Me**: only the user's own collections. A real,
  materialised study, but refreshed only when that user's collections change —
  never by the all-studies sweeps (they skip ``SYSTEM`` defs).
* ``__me_plus__{username}`` — **Everyone & Me**: the user's collections
  combined with the site default study. Never materialised: its def carries
  ``COMPOSE`` and the web layer assembles the frame at load time from the
  default study's artifacts plus the ``__me__`` overlay
  (see services/study_data.py).

This module is the single owner of the pair's lifecycle. Callers:

* ``run_ingest_refresh`` after a donation is registered,
* ``collection_accounts.set_collection_owner`` when an admin re-links,
* ``run_collection_delete`` after collections are removed,
* the account-deletion path in ``auth``.

Every entry point is defensive — a failure here must never fail the caller.
"""

import threading

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.logging_setup import get_logger
from fyp.studies import (
    STUDY_ARTIFACT_SUFFIXES,
    SYSTEM_PARTICIPANT,
    init_study_defs,
    is_composed_study,
    participant_me_name,
    participant_plus_name,
    save_study_defs,
)

logger = get_logger(__name__)

DISPLAY_JUST_ME = "Just Me"
DISPLAY_EVERYONE_AND_ME = "Everyone & Me"


def _me_def(username: str, cids: list[str]) -> dict:
    # USER_ACCESS must never be empty: migrate_user_access_defaults grants
    # every non-admin role to empty-access studies on boot (it skips SYSTEM
    # defs too, but the non-empty list is the primary guarantee).
    return {
        "SYSTEM": SYSTEM_PARTICIPANT,
        "OWNER": username,
        "DISPLAY_NAME": DISPLAY_JUST_ME,
        "SELECTED_COLLECTIONS": list(cids),
        "START_DATE": "",
        "END_DATE": "",
        "SAMPLE_FRAME": "off",
        "USER_ACCESS": [username],
    }


def _plus_def(username: str, cids: list[str]) -> dict:
    # SELECTED_COLLECTIONS lists only the user's own collections — the default
    # study's come from composition at read time, so the def never goes stale
    # when the admin changes or edits the default study.
    return {
        "SYSTEM": SYSTEM_PARTICIPANT,
        "OWNER": username,
        "DISPLAY_NAME": DISPLAY_EVERYONE_AND_ME,
        "COMPOSE": {"base": "__default__", "overlay": "self"},
        "SELECTED_COLLECTIONS": list(cids),
        "START_DATE": "",
        "END_DATE": "",
        "SAMPLE_FRAME": "off",
        "USER_ACCESS": [username],
    }


def _remove_study(name: str, *, drop_artifacts: bool) -> bool:
    defs = fyp_cf.get("study_defs", {})
    existed = name in defs
    if existed:
        del defs[name]
    if drop_artifacts:
        for suffix in STUDY_ARTIFACT_SUFFIXES:
            try:
                data_io.remove(storage_location="cache", filename=f"{name}{suffix}")
            except Exception:
                pass
    return existed


def ensure_participant_studies(username: str, *, log=logger.info) -> dict:
    """Create/update (or remove) ``username``'s study pair from the ownership store.

    Reads the collection links fresh, upserts the two defs when the user owns
    collections, and removes the pair (defs + Just Me artifacts) when they own
    none. Saves ``studies.json`` only when something changed.

    Returns ``{"me_changed": bool, "removed": bool, "collections": int}`` —
    ``me_changed`` means the Just Me dataset needs a refresh.
    """
    from web_interface.collection_accounts import collections_for_user

    username = (username or "").strip()
    if not username:
        return {"me_changed": False, "removed": False, "collections": 0}

    cids = [str(c) for c in collections_for_user(username, fresh=True)]

    init_study_defs()
    defs = fyp_cf["study_defs"]
    me_name = participant_me_name(username)
    plus_name = participant_plus_name(username)

    if not cids:
        removed_me = _remove_study(me_name, drop_artifacts=True)
        removed_plus = _remove_study(plus_name, drop_artifacts=False)
        if removed_me or removed_plus:
            save_study_defs()
            log(f"Participant studies removed for {username} (no owned collections).")
        return {"me_changed": False, "removed": removed_me or removed_plus,
                "collections": 0}

    changed = False
    me_changed = False
    for name, template in ((me_name, _me_def(username, cids)),
                           (plus_name, _plus_def(username, cids))):
        existing = defs.get(name)
        if not isinstance(existing, dict):
            defs[name] = template
            changed = True
            if name == me_name:
                me_changed = True
            continue
        # Preserve server-computed keys (stats, last_updated, cached charts);
        # overwrite every definitional key so the pattern stays canonical even
        # if an old def predates a template change.
        before_collections = sorted(map(str, existing.get("SELECTED_COLLECTIONS") or []))
        for key, value in template.items():
            if existing.get(key) != value:
                existing[key] = value
                changed = True
        if name == me_name and before_collections != sorted(cids):
            me_changed = True

    if changed:
        save_study_defs()
        log(f"Participant studies ensured for {username} "
            f"({len(cids)} collection(s), refresh_needed={me_changed}).")
    return {"me_changed": me_changed, "removed": False, "collections": len(cids)}


def dispatch_me_refresh(username: str, *, wait: bool = False, log=logger.info) -> bool:
    """Build/refresh the Just Me dataset for ``username``.

    On Cloud Run this dispatches a ``study_refresh`` Cloud Task (the same
    path a study save uses). Locally it either runs the refresh synchronously
    (``wait=True`` — for worker processes, where a daemon thread would die
    with the process) or on a background thread (web process).
    """
    study_name = participant_me_name(username)
    task_args = {
        "study_name": study_name,
        "refresh_pca": True,
        "refresh_metadata": True,
    }

    from web_interface.task_status import is_cloud_run

    if is_cloud_run():
        from web_interface.process_manager import start_process

        ok, msg = start_process("study_refresh", None, task_args=task_args,
                                started_by="system (participant studies)")
        if not ok:
            log(f"Just Me refresh dispatch failed for {username}: {msg}")
        return ok

    from web_interface.run_study_refresh import run_study_refresh
    from web_interface.task_status import LocalThreadStatusReporter

    def _run():
        reporter = LocalThreadStatusReporter(f"study_refresh__{study_name}")
        try:
            run_study_refresh(reporter=reporter, task_args=task_args)
            reporter.complete()
        except Exception as exc:
            log(f"Just Me refresh failed for {username}: {exc}")
            try:
                reporter.fail(str(exc))
            except Exception:
                pass

    if wait:
        _run()
    else:
        threading.Thread(target=_run, daemon=True,
                         name=f"study_refresh__{study_name}").start()
    return True


def _account_has_logged_in(username: str) -> bool:
    """True when the account exists and has logged in at least once.

    The dormancy gate: most donation-linked accounts (placeholders, AIO-minted
    participant accounts) never log in, and building studies for them would be
    pure waste. Fails CLOSED — an unreadable user store defers creation rather
    than mass-creating; the login trigger re-runs the check on every login, so
    an engaged user always converges on having their pair.
    """
    try:
        # Function-level import: the security module builds the Flask login
        # manager, which worker processes must not pull in at import time
        # (same pattern as collection_accounts._um).
        from web_interface.security import user_manager

        user = user_manager.get_user(username)
        return bool(user and user.last_login)
    except Exception:
        return False


def sync_for_cids(cids, *, usernames=(), wait: bool = False, log=logger.info) -> list[str]:
    """Reconcile participant studies for the owners of ``cids``.

    Maps the collection ids to their owners through the (fresh) ownership
    store, ensures each owner's pair, and dispatches a Just Me refresh for
    every owner whose collection set changed. ``usernames`` adds owners the
    id map can no longer reach — e.g. the *previous* owner of a re-linked
    collection. Returns the affected usernames.

    Lazy provisioning: an owner with NO pair yet only gets one created once
    their account has logged in at least once (``ensure_on_login`` is the
    trigger that catches them when they do). Owners who already carry a pair
    are always reconciled, so grow/shrink/remove keeps working for everyone.
    """
    from web_interface.collection_accounts import load_owner_map

    owner_map = load_owner_map(fresh=True)
    owners = {owner_map.get(str(c)) for c in (cids or [])}
    owners.update(usernames or ())
    owners = sorted(o for o in owners if o)

    if not owners:
        return []
    init_study_defs()
    defs = fyp_cf.get("study_defs") or {}

    affected: list[str] = []
    for username in owners:
        if participant_me_name(username) not in defs and not _account_has_logged_in(username):
            log(f"Participant studies deferred for {username} (account has never logged in).")
            continue
        try:
            result = ensure_participant_studies(username, log=log)
        except Exception as exc:
            log(f"Participant-study sync failed for {username}: {exc}")
            continue
        affected.append(username)
        if result["me_changed"]:
            try:
                dispatch_me_refresh(username, wait=wait, log=log)
            except Exception as exc:
                log(f"Just Me refresh dispatch failed for {username}: {exc}")
    return affected


def ensure_on_login(username: str) -> None:
    """Provision/reconcile the pair for a user who just logged in.

    The login-time half of lazy provisioning: (a) do they own donated
    collections, (b) does their Just Me study exist and match — both answered
    by ``ensure_participant_studies``, which is idempotent and reads two small
    JSONs, so running it on every login is cheap. Runs synchronously in the
    login request (a post-response daemon thread would stall under Cloud
    Run's CPU throttling); the heavy build itself is still dispatched, not
    run inline. Never fails the login.
    """
    try:
        sync_for_cids([], usernames=[username])
    except Exception as exc:
        logger.warning(f"Login-time participant-study check failed for {username}: {exc}")


def refresh_stale_participant_studies(*, wait: bool = True, log=logger.info) -> list[str]:
    """Safety net: reconcile EVERY participant pair against the ownership store.

    Covers drift the targeted triggers can miss (a link edited while a sync
    failed, a def surviving its owner). Cheap when nothing changed — each
    owner costs one in-memory comparison; refreshes dispatch only for owners
    whose collection set actually moved. Run from the untargeted recode sweep.
    """
    from web_interface.collection_accounts import load_owner_map

    owner_map = load_owner_map(fresh=True)
    owners = {u for u in owner_map.values() if u}
    init_study_defs()
    def_owners = {
        cfg.get("OWNER") for cfg in (fyp_cf.get("study_defs") or {}).values()
        if isinstance(cfg, dict) and cfg.get("SYSTEM")
    }
    all_owners = sorted(o for o in owners | def_owners if o)
    return sync_for_cids([], usernames=all_owners, wait=wait, log=log)


def is_participant_plus_def(config) -> bool:
    """Convenience re-export for workers that must skip composed defs."""
    return is_composed_study(config)
