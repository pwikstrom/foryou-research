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
        user_access = config.get("USER_ACCESS")
        if isinstance(user_access, list) and user_access:
            continue
        config["USER_ACCESS"] = list(grant_roles)
        migrated += 1
        logger.info(f"USER_ACCESS migration: granted {grant_roles} on study '{study_name}'.")

    if migrated:
        save_study_defs()
    return migrated





