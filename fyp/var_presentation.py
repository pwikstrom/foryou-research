#!/usr/bin/env python3
"""Admin-editable presentation store for the variable schema.

The final piece of the var_schema.csv retirement: the four ``web_*_prio``
columns are ON/OFF membership flags per UI surface (filter / timeline / viz /
display), and they are the only admin-editable payload the CSV still carried.
This module owns them as ``var_presentation.json`` (storage location
``"users"``, next to ``admin_settings.json``):

    {"version": 1,
     "surfaces": {"filter": [...], "timeline": [...], "viz": [...], "display": [...]},
     "updated_at": "...", "updated_by": "..."}

Membership only — ordering stays derived (section → categorical-before-numeric
→ alphabetical, see ``data_service.load_schema_metadata``). Presentation edits
can never change the study hash (``web_*`` columns are excluded from
``compute_var_schema_hash``).

``load_var_schema`` synthesizes the in-memory var_schema from the contracts +
version registries and fills the prio columns from this store; when the store
is missing it is seeded once from the legacy ``var_schema.csv`` prios
(idempotent — concurrent seeding writes identical content).
"""

import datetime as _dt
import hashlib
import json

FILENAME = "var_presentation.json"
LOCATION = "users"
SURFACES = ("filter", "timeline", "viz", "display")

# Surface name → the legacy var_schema prio column it replaces.
SURFACE_TO_PRIO_COLUMN = {
    "filter": "web_filter_prio",
    "timeline": "web_timeline_prio",
    "viz": "web_viz_prio",
    "display": "web_display_prio",
}


class PresentationConflict(Exception):
    """Raised when a save's expected etag does not match the stored state."""






def _data_io():
    """Lazy fyp.data_io accessor (avoids the fyp_config import cycle)."""
    import fyp.data_io as data_io

    return data_io






def empty_presentation() -> dict:
    """Return a fresh, empty presentation payload."""
    return {"version": 1, "surfaces": {s: [] for s in SURFACES}}






def load_presentation() -> dict | None:
    """Load the presentation store, or None when it does not exist yet.

    Never raises — a read failure degrades to None so the caller can fall back
    to the legacy CSV prios.
    """
    try:
        if _data_io().exists(storage_location=LOCATION, filename=FILENAME):
            payload = _data_io().load_json(storage_location=LOCATION, filename=FILENAME)
            if isinstance(payload, dict) and isinstance(payload.get("surfaces"), dict):
                return payload
    except Exception as e:
        print(f"WARNING: var_presentation store unreadable ({e}).")
    return None






def compute_presentation_etag(payload: dict | None = None) -> str:
    """Deterministic etag of the presentation content (sha256 of canonical JSON)."""
    if payload is None:
        payload = load_presentation()
    if payload is None:
        return "missing"
    surfaces = {s: sorted(payload.get("surfaces", {}).get(s, []) or []) for s in SURFACES}
    canonical = json.dumps(surfaces, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]






def save_presentation(
    surfaces: dict,
    expected_etag: str | None = None,
    updated_by: str = "",
) -> dict:
    """Persist the per-surface membership lists; returns ``{"etag": ...}``.

    Args:
        surfaces: ``{surface: [variable_name, ...]}`` — unknown surface keys are
            rejected; each list is deduplicated and sorted for stable storage.
        expected_etag: when given, the save is refused (PresentationConflict)
            if the stored content has changed since the caller read it.
        updated_by: username recorded in the payload for audit.

    Raises:
        PresentationConflict: etag mismatch (concurrent edit).
        ValueError: malformed surfaces payload.
    """
    if not isinstance(surfaces, dict):
        raise ValueError("surfaces must be an object")
    unknown = [s for s in surfaces if s not in SURFACES]
    if unknown:
        raise ValueError(f"unknown surfaces: {unknown}")
    for s, names in surfaces.items():
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError(f"surface {s!r} must be a list of variable names")

    if expected_etag is not None and expected_etag != compute_presentation_etag():
        raise PresentationConflict(
            "presentation store changed since it was loaded — reload and retry"
        )

    current = load_presentation() or empty_presentation()
    merged = {
        s: sorted(set(surfaces.get(s, current.get("surfaces", {}).get(s, []) or [])))
        for s in SURFACES
    }
    payload = {
        "version": 1,
        "surfaces": merged,
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "updated_by": updated_by,
    }
    _data_io().save_json(data=payload, storage_location=LOCATION, filename=FILENAME)
    return {"etag": compute_presentation_etag(payload)}






def seed_from_var_schema_frame(vs) -> dict | None:
    """Derive the presentation payload from a legacy var_schema frame's prios.

    Membership = any non-blank numeric value in the corresponding ``web_*_prio``
    column (the on/off convention). Returns the payload, or None when the frame
    carries no usable prio columns. Does NOT save — the caller decides.
    """
    import pandas as pd

    if vs is None or getattr(vs, "empty", True) or "variable_name" not in vs.columns:
        return None
    surfaces: dict = {}
    found_any = False
    for surface, col in SURFACE_TO_PRIO_COLUMN.items():
        if col in vs.columns:
            found_any = True
            on = pd.to_numeric(vs[col], errors="coerce").notna()
            surfaces[surface] = sorted(vs.loc[on, "variable_name"].astype("string").tolist())
        else:
            surfaces[surface] = []
    if not found_any:
        return None
    return {"version": 1, "surfaces": surfaces}
