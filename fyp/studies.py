#!/usr/bin/env python3
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf
from fyp.logging_setup import get_logger

logger = get_logger(__name__)


def init_study_defs():
    global fyp_cf

    if data_io.exists(storage_location="recoded", filename="studies.json"):
        study_defs = data_io.load_json(storage_location="recoded", filename="studies.json")
    else:
        logger.warning("Unable to init study defs from disk. Setting to empty dict.")
        study_defs = {}

    fyp_cf["study_defs"] = study_defs
    logger.info(f"Loaded {len(study_defs)} study definitions. OK.")
    


def save_study_defs():

    if "study_defs" not in fyp_cf:
        init_study_defs()

    data_io.save_json(data = fyp_cf["study_defs"], storage_location="recoded", filename="studies.json")





