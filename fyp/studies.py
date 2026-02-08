#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik
Date: 
"""


import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf



def init_study_defs():
    global fyp_cf

    if data_io.exists(storage_location="studies", filename="studies.json"):
        study_defs = data_io.load_json(storage_location="studies", filename="studies.json")
    else:
        print(f"Unable to init study defs from disk. Setting to empty dict.")
        study_defs = {}

    fyp_cf["study_defs"] = study_defs
    print(f"Loaded {len(study_defs)} study definitions. OK.")
    


def save_study_defs():

    if "study_defs" not in fyp_cf:
        init_study_defs()

    data_io.save_json(data = fyp_cf["study_defs"], storage_location="studies", filename="studies.json")





