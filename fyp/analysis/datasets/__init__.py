"""Building the analysis datasets: load, sample, merge, and keep them fresh.

``common`` (shared names), ``loading`` (activity, scrapes, annotations),
``sampling`` (study day cells and thresholds), ``merge`` (a study's recoded
frame), ``refresh`` (fingerprints, sidecars, the refresh plan) and
``enrichment_status`` (the per-item status table and the consolidation that
builds it). ``fyp.analysis.organize_datasets`` holds the entry points and
re-exports these modules' public names for callers of that path.
"""
