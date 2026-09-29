"""The Sessions tab's build: session segmentation, the refresh planner, and publishing.

``inputs`` (artifact names, parameters, loaders, coverage), ``segment``
(episodes, windows, per-session records, the fork pool), ``plan`` (what a
refresh must rebuild) and ``publish`` (batch builds, shards, the live
artifacts). ``fyp.analysis.session_explorer`` re-exports their public names
for callers of that path.
"""
