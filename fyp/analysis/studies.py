#!/usr/bin/env python3
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import fyp.data_io as data_io
from fyp.logging_setup import get_logger

logger = get_logger(__name__)




def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.fyp_config import fyp_cf

    return fyp_cf


# --- Participant (system-managed) studies -----------------------------------
#
# Every user who owns donated collections gets a fixed pair of auto-managed
# studies (see web_interface/services/participant_studies.py, which owns the
# lifecycle):
#
#   __me__{username}       "Just Me"        — only the user's own collections.
#                          Materialised like any other study, but refreshed
#                          only when that user's collections change — never by
#                          the all-studies sweeps.
#   __me_plus__{username}  "Everyone & Me"  — the user's collections combined
#                          with the site default study. NEVER materialised: the
#                          web layer composes its frame at load time from the
#                          default study's artifacts plus the __me__ overlay
#                          (the def carries COMPOSE to mark this).
#
# Defs carry SYSTEM="participant" (auto-managed marker), OWNER (the username),
# DISPLAY_NAME (what the UI shows) and USER_ACCESS=[username] — the access
# list must never be empty or migrate_user_access_defaults would open the
# study to every role on the next boot.

SYSTEM_PARTICIPANT = "participant"
PARTICIPANT_ME_PREFIX = "__me__"
PARTICIPANT_PLUS_PREFIX = "__me_plus__"
# Reserved namespace for system-managed study names; human saves are refused.
SYSTEM_STUDY_NAME_PREFIX = "__"


def participant_me_name(username: str) -> str:
    return f"{PARTICIPANT_ME_PREFIX}{username}"


def participant_plus_name(username: str) -> str:
    return f"{PARTICIPANT_PLUS_PREFIX}{username}"


def is_system_study(config) -> bool:
    """True when ``config`` is an auto-managed (system) study definition."""
    return isinstance(config, dict) and bool(config.get("SYSTEM"))


def is_composed_study(config) -> bool:
    """True when ``config`` describes a composed study — one with no artifacts
    of its own, whose frame the web layer assembles at load time (COMPOSE)."""
    return isinstance(config, dict) and bool(config.get("COMPOSE"))


# Per-study cached artifacts, all in the "cache" storage location. Keep in
# sync with the run_* workers that write them (study/recode refresh, pca
# refresh, meta refresh, sequence refresh, methods note). Consumed by study
# delete/rename (routes/management/studies.py) and the participant-study
# lifecycle (services/participant_studies.py).
STUDY_ARTIFACT_SUFFIXES = [
    "_recoded.parquet",
    "_recoded.meta.json",
    "_explorer_metadata.json",
    "_comp_interpretations.json",
    "_PCA.parquet",
    "_corr_stats.json",
    "_methods.json",
    "_sequence.parquet",
    "_sequence_summary.json",
]




def init_study_defs():

    if data_io.exists(storage_location="recoded", filename="studies.json"):
        study_defs = data_io.load_json(storage_location="recoded", filename="studies.json")
    else:
        logger.warning("Unable to init study defs from disk. Setting to empty dict.")
        study_defs = {}

    _cf()["study_defs"] = study_defs
    logger.info(f"Loaded {len(study_defs)} study definitions. OK.")



def save_study_defs():

    if "study_defs" not in _cf():
        init_study_defs()

    data_io.save_json(data = _cf()["study_defs"], storage_location="recoded", filename="studies.json")




def migrate_user_access_defaults(grant_roles: list[str]) -> int:
    """One-time backfill for the empty-means-none USER_ACCESS flip (S4).

    Before S4, a study with a missing/empty ``USER_ACCESS`` list was visible
    to every logged-in user on the analysis tabs. Access is now deny-by-
    default, so every study that relied on the old default gets an explicit
    grant of ``grant_roles`` (the caller passes the role names that existed
    at migration time, minus admin — which bypasses the check anyway — and
    minus deliberately-restricted roles such as ``student``). Existing users
    therefore keep exactly the access they had, while roles created later
    start with no access until a study is explicitly shared.

    Idempotent: studies that already carry a non-empty list are untouched,
    so the second boot finds nothing to do.

    Args:
        grant_roles: Role names to write into each unshared study.

    Returns:
        The number of studies migrated.
    """
    if "study_defs" not in _cf():
        init_study_defs()

    migrated = 0
    for study_name, config in _cf()["study_defs"].items():
        # System-managed studies are created with USER_ACCESS=[owner] and must
        # never receive a role-wide grant — that would share a participant's
        # personal data with every account.
        if is_system_study(config):
            continue
        user_access = config.get("USER_ACCESS")
        if isinstance(user_access, list) and user_access:
            continue
        config["USER_ACCESS"] = list(grant_roles)
        migrated += 1
        logger.info(f"USER_ACCESS migration: granted {grant_roles} on study '{study_name}'.")

    if migrated:
        save_study_defs()
    return migrated





