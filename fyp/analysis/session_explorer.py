"""Session-level data quality and focused-episode ("binge") exploration.

Production port of the embedding-entropy study's episode segmenter. For every
collection it reduces the persistent viewing sessions (the ingest-assigned
``session_id``, 900 s gap) to two artifacts the Sessions tab reads:

* ``cache/sessions_index.parquet`` — one row per session: data-quality
  coverage (what share of the session's videos are scraped / annotated /
  embedded) plus embedding-entropy focus metrics (the minimum sliding-window
  mean pairwise cosine distance over :data:`WINDOW_N` distinct embedded
  videos).
* ``cache/session_episodes.parquet`` — one row per detected **focus episode**
  ("binge" in the UI): a maximal within-session run whose content stays
  semantically focused on the embeddings, with its geometry (stationary binge
  vs directed drift), content attribution (niche / author / valence), and the
  ordered member list the UI's side-by-side players render.
* ``cache/session_windows.parquet`` — one row per **low-entropy window**: up
  to :data:`MAX_WINDOWS` non-overlapping :data:`WINDOW_N`-video windows per
  session with the smallest mean pairwise cosine distance (the session's
  ``min_window_cosdist`` is window 0's score), detector-independent of the
  episode segmentation.

Segmentation rule (per session, on embedded plays, distinct videos): grow the
current episode while the next *distinct* video's mean cosine distance to the
centroid of the last :data:`MEM` members ≤ :data:`CUT`. Up to :data:`MAX_SKIP`
consecutive off-theme videos are tolerated without ending the episode (an ad
mid-binge is the motivating case); they are counted as ``n_skipped`` but are
never members and never enter the centroid. Beyond that the episode closes
(kept if ≥ :data:`MIN_VIDEOS` distinct videos over ≥ :data:`MIN_MINUTES`) and
the scan rewinds to the first tolerated video so it can open the next episode.
``session_id`` boundaries hard-break episodes; repeated plays of a video
already in the episode extend its span but are not new members (a rewatch loop
must not fake a binge).

The artifacts are **global** (all collections) and study-scoped at query time:
per-study caches are sampled and shred sequences, so everything here reads the
full ``recoded/collections_recoded.parquet``. The embedding store is
model-scoped — every read passes an explicit ``model`` so vectors from
different embedding models are never mixed.

The implementation lives in :mod:`fyp.analysis.sessions` (``inputs``,
``segment``, ``plan``, ``publish``); this module re-exports its public names
so callers of this path keep working. First-party code imports from the
submodules.
"""

from fyp.analysis.sessions.inputs import (  # noqa: F401
    ARTIFACT_LOCATION,
    CORPUS_MEAN_PREFIX,
    COVERAGE_PAD_DAYS,
    CUT,
    EPISODES_FILE,
    FLICK_SECONDS,
    MAX_SKIP,
    MAX_VECTORS_PER_LINK,
    MAX_WINDOWS,
    MEM,
    META_FILE,
    MIN_MINUTES,
    MIN_VIDEOS,
    PLAYS_FILE,
    PLAYS_ROW_GROUP,
    PROGRESS_PREFIX,
    SESSION_CHUNK_PLAYS,
    SESSIONS_FILE,
    SHARD_PREFIXES,
    TREND_EXCLUDE,
    WINDOW_N,
    WINDOWS_FILE,
    annotation_corpus_fingerprint,
    collections_meta_block,
    compute_coverage_spec,
    coverage_mask,
    default_params,
    discover_collections,
    discover_covered_collections,
    enrichment_id_sets,
    load_corpus_mean,
    load_directional_block,
    load_directional_store,
    load_plays,
    load_story_texts,
    load_video_features,
    merge_intervals,
    save_corpus_mean,
    trend_numeric_columns,
    vector_cache_enabled,
)
from fyp.analysis.sessions.plan import (  # noqa: F401
    REBASELINE_FRACTION_DEFAULT,
    annotation_corpus_max_ts,
    annotation_items_changed_since,
    collections_containing,
    compute_refresh_plan,
    enrichment_change_scope,
    new_vector_item_ids,
    rebaseline_fraction,
    shards_appended_only,
)
from fyp.analysis.sessions.publish import (  # noqa: F401
    PLAY_TEXT_CAP,
    attach_play_texts,
    build_artifacts,
    build_batch,
    format_batch_timing,
    merge_publish_artifacts,
    plays_table,
    publish_artifacts,
    sessions_schema,
    shard_filename,
    sweep_stale_run_files,
    write_batch_shards,
)
from fyp.analysis.sessions.segment import (  # noqa: F401
    build_collection,
    build_session_group,
    episode_record,
    low_entropy_windows,
    resolve_workers,
    segment_session,
    session_record,
)
