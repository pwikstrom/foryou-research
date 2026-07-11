"""Study access control, per-user variable composition, schema metadata.

Pure moves from web_interface/data_service.py (Phase 7c)."""


import pandas as pd

import fyp.data_io as data_io
from fyp.fyp_config import fyp_cf

from .study_data import SECTION_ORDER, _CAT_SCALES


# --- Explorer State ---


def get_accessible_studies(username: str, role: str, is_admin: bool,
                           include_stats: bool = False) -> list:
    """Return study names (or dicts with stats) that the user has access to.

    Args:
        username: Current user's username.
        role: Current user's role.
        is_admin: Whether the user is an admin.
        include_stats: When True, return ``[{"name": ..., "stats": {...}}]``
            instead of a flat list of names. The stats dict is augmented
            with ``has_pca`` and ``has_timelines`` booleans so the UI can
            gate the Correlations and Timelines tabs per study.
    """
    from fyp.studies import init_study_defs

    if 'study_defs' not in fyp_cf:
        init_study_defs()

    accessible_studies = []

    # When stats are requested we also need to know which studies have
    # PCA scores and which have timelines — both gate tab availability in
    # the UI. List the cache once so we can answer via set membership
    # instead of issuing one exists()/listdir() call per study/collection.
    # Timeline availability mirrors what the Timelines dropdown surfaces:
    # at least one collection in the study must have
    # active_days >= MIN_ACTIVE_DAYS_FOR_TIMELINE. Checking file presence
    # alone was insufficient — stray cache files from other studies can
    # falsely mark a study as timeline-capable even when every collection
    # in it falls below the analysable-length threshold.
    cache_files: set[str] = set()
    timeline_capable_cids: set[str] = set()
    if include_stats:
        try:
            cache_files = set(data_io.listdir(storage_location="cache"))
        except Exception:
            cache_files = set()
        try:
            from fyp.organize_datasets import COLLECTIONS_LABEL
            from fyp.timeline_analysis import MIN_ACTIVE_DAYS_FOR_TIMELINE
            meta_df = data_io.load_parquet_selective(
                storage_location="recoded",
                filename=f"{COLLECTIONS_LABEL}_metadata.parquet",
                columns=["('personas', 'active_days')", "active_days"],
                set_index='collection_id',
            )
            if meta_df is not None and not meta_df.empty:
                active_days_col = None
                if ('personas', 'active_days') in meta_df.columns:
                    active_days_col = ('personas', 'active_days')
                elif 'active_days' in meta_df.columns:
                    active_days_col = 'active_days'
                if active_days_col is not None:
                    df_reset = meta_df.reset_index()
                    ad_series = pd.to_numeric(df_reset[active_days_col], errors='coerce')
                    capable_mask = ad_series >= MIN_ACTIVE_DAYS_FOR_TIMELINE
                    timeline_capable_cids = set(
                        df_reset.loc[capable_mask, 'collection_id']
                        .dropna().astype(str).str.strip().tolist()
                    )
        except Exception:
            timeline_capable_cids = set()

    if 'study_defs' in fyp_cf:
        for study_name, study_config in fyp_cf['study_defs'].items():
            # 1. Admin Override
            if is_admin:
                has_access = True
            else:
                user_access = study_config.get('USER_ACCESS')

                # 2. Missing or Empty => Default Allow
                if not user_access or not isinstance(user_access, list) or 'all' in user_access or role in user_access or username in user_access:
                    has_access = True
                else:
                    has_access = False

            if has_access:
                # Data Integrity Checks
                if not data_io.exists(storage_location="cache", filename=f"{study_name}_recoded.parquet"):
                    continue

                stats = study_config.get('stats', {})
                # Defensive: a bad client save could persist stats as a string
                # (e.g. "[object Object]"). Treat anything non-dict as empty
                # so the listing endpoint keeps working for other studies.
                if not isinstance(stats, dict):
                    stats = {}
                if stats.get('unique_videos', 0) <= 0:
                    continue

                if include_stats:
                    stats = dict(stats)
                    stats['has_pca'] = f"{study_name}_PCA.parquet" in cache_files
                    # A study "has timelines" only when at least one of its
                    # collections is long enough to analyse (active_days >=
                    # threshold) AND has an actual cached timeline parquet.
                    # Both gates matter: without the length check, a stale
                    # cache file would re-enable the tab for a study whose
                    # collections are all too short; without the file check,
                    # collections that qualify on paper but whose timelines
                    # have never been generated would appear available.
                    selected = study_config.get('SELECTED_COLLECTIONS', []) or []
                    stats['has_timelines'] = any(
                        (cid_clean := str(cid).strip()) in timeline_capable_cids
                        and f"timeline_{cid_clean}_day.parquet" in cache_files
                        for cid in selected
                    )
                    accessible_studies.append({"name": study_name, "stats": stats})
                else:
                    accessible_studies.append(study_name)

    if include_stats:
        return sorted(accessible_studies, key=lambda s: s["name"])
    return sorted(accessible_studies)




def compose_effective_variables(global_list, prefs, all_order, available=None):
    """Compose a per-user effective variable list for one surface.

    ``effective = (global ∪ include) − exclude``, ordered by ``all_order`` (the
    canonical derived order). Unknown names in the prefs are ignored, so stored
    preferences survive schema evolution. When ``available`` is given, includes
    are clipped to it (used by timelines, where a variable needs aggregated
    data to be renderable).

    Args:
        global_list: the admin-set global ON list for the surface.
        prefs: ``{"include": [...], "exclude": [...]}`` or None/empty.
        all_order: full canonical-ordered candidate list.
        available: optional iterable of variables that actually have data.

    Returns:
        list[str] in canonical order.
    """
    prefs = prefs or {}
    include = set(prefs.get("include") or [])
    exclude = set(prefs.get("exclude") or [])
    base = set(global_list) | include
    base -= exclude
    if available is not None:
        avail = set(available)
        # Global members stay even without an availability entry (back-compat);
        # only user includes are clipped to what has data.
        base = {v for v in base if v in avail or v in set(global_list)}
    ordered = [v for v in all_order if v in base]
    # Preserve anything not in all_order (e.g. synthetic vars like
    # machine_state prepended by the server) in its original position.
    extras = [v for v in global_list if v not in all_order and v in base]
    return extras + ordered






def load_schema_metadata(metadata):
    """Helper to load and inject schema metadata (priorities, descriptions, accepted_labels) from CSV."""
    try:
        #var_schema_path = PROJECT_ROOT / "config" / "var_schema.csv"
        if "var_schema" in fyp_cf and not fyp_cf["var_schema"].empty:
            schema_df = fyp_cf["var_schema"].copy()

            # A variable's position in every web list is derived, not hand-ranked:
            # (1) hard-coded section order, (2) categorical before numerical (from
            # ``scale``), (3) alphabetical by display name. The four ``web_*_prio``
            # columns are read as on/off membership only — any non-blank value
            # includes the variable; the numeric value no longer affects order.
            if 'section' in schema_df.columns:
                _sections = schema_df['section'].astype('string').fillna('')
            else:
                _sections = pd.Series('', index=schema_df.index)
            if 'scale' in schema_df.columns:
                _scales = schema_df['scale'].astype('string').fillna('').str.strip().str.lower()
            else:
                _scales = pd.Series('', index=schema_df.index)
            if 'display_name' in schema_df.columns:
                _names = schema_df['display_name'].astype('string')
            else:
                _names = pd.Series(pd.NA, index=schema_df.index)
            _names = _names.fillna(schema_df['variable_name'].astype('string')).fillna('').str.strip().str.lower()

            schema_df['_sec_rank'] = _sections.map(
                lambda s: SECTION_ORDER.index(s) if s in SECTION_ORDER else len(SECTION_ORDER))
            schema_df['_section'] = _sections
            schema_df['_cat_num'] = _scales.map(lambda s: 0 if s in _CAT_SCALES else 1)
            schema_df['_sort_name'] = _names
            order_cols = ['_sec_rank', '_section', '_cat_num', '_sort_name']

            def _ordered(prio_col):
                """Return ON variables for ``prio_col`` in canonical sort order."""
                if prio_col not in schema_df.columns:
                    return []
                is_on = pd.to_numeric(schema_df[prio_col], errors='coerce').notna()
                return schema_df[is_on].sort_values(order_cols)['variable_name'].tolist()

            metadata['section_order'] = list(SECTION_ORDER)
            metadata['display_priority'] = _ordered('web_display_prio')
            metadata['viz_priority'] = _ordered('web_viz_prio')
            metadata['timeline_priority'] = _ordered('web_timeline_prio')
            metadata['filter_priority'] = _ordered('web_filter_prio')
            # Full candidate list in the same canonical order, regardless of the
            # on/off flags. Per-user variable preferences compose against this
            # (effective = (global ∪ include) − exclude) client-side.
            metadata['all_variables_order'] = (
                schema_df.sort_values(order_cols)['variable_name'].tolist())

            if 'section' not in schema_df.columns:
                schema_df['section'] = 'General'
            if 'description' not in schema_df.columns:
                schema_df['description'] = ''
            
            schema_df['section'] = schema_df['section'].fillna('General')
            schema_df['description'] = schema_df['description'].fillna('')
            
            schema_map = {}
            for _, row in schema_df.iterrows():
                var_name = row['variable_name']
                schema_map[var_name] = {
                    "section": str(row['section']),
                    "description": str(row['description'])
                }
                
                # Parse Accepted Labels for Closed Tags
                if 'accepted_labels' in row:
                    accepted = str(row['accepted_labels'])
                    if accepted and accepted.lower() != 'nan' and accepted.startswith('[') and accepted.endswith(']'):
                        content = accepted[1:-1]
                        if content.strip():
                            labels = [x.strip() for x in content.split(',')]
                            schema_map[var_name]['accepted_labels'] = labels
                
                # Add Display Name
                if 'display_name' in row:
                    dname = str(row['display_name'])
                    if dname and dname.lower() != 'nan' and dname.strip():
                        schema_map[var_name]['display_name'] = dname.strip()

                # On/off membership flag the viewer's metadata panel reads to
                # decide whether to render a variable (the value itself is no
                # longer used for ordering, so any non-blank entry counts as on).
                if 'web_display_prio' in row:
                    prio = pd.to_numeric(row['web_display_prio'], errors='coerce')
                    if pd.notna(prio):
                         schema_map[var_name]['web_display_prio'] = float(prio)

                # Scale drives the timeline multi-label share denominator
                # (collection => multi-label) now that web_viz_multi_label is
                # derived rather than stored.
                if 'scale' in row:
                    sval = row['scale']
                    if pd.notna(sval):
                        schema_map[var_name]['scale'] = str(sval).strip().lower()

            metadata['schema_map'] = schema_map
                
        else:
            # Only reset if keys missing? Or always reset? 
            # If CSV missing, we might want to keep existing if available?
            # But here we assume CSV is source of truth.
            metadata['display_priority'] = []
            metadata['filter_priority'] = []
            metadata['schema_map'] = {}
    except Exception as e:
        print(f"Error loading priority list: {e}")
        # Don't overwrite with empty if error?
    return metadata


