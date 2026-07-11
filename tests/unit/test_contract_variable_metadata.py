"""Unit tests for contract-sourced Gemini variable metadata (synthesized schema).

``var_schema.csv`` is retired: ``fyp_config.load_var_schema`` synthesizes the
in-memory schema from the four contract TOMLs (+ the version registries' legacy
snapshots) overlaid onto an empty typed skeleton (``VAR_SCHEMA_COLUMNS``), then
fills the four ``web_*_prio`` membership columns from the presentation store
(``var_presentation.json``). ``fyp_config._apply_contract_variable_metadata`` is
the annotation overlay: on the skeleton it INJECTS every contract-owned row.
Pins:

  * on an empty typed skeleton the overlay injects every contract-owned column
    with the contract's role/scale/display_name/description, exactly once, and
    forces section="AI Annotations";
  * legacy fields owned only by PAST annotation versions (the registry's
    per-version field_metadata union, minus the current contract) are injected
    too, from their registry snapshots — superseded fields stay contract-owned;
    the current contract wins wherever both own a column;
  * the overlay is idempotent (a second pass changes nothing, no duplicate rows);
  * contract_column_metadata keys equal the flattener's output columns (rename +
    audio_summary prefix-strip), guarding the column-name mapping against drift;
  * the LIVE synthesized schema carries every owned row exactly once, keeps
    non-Gemini rows out of "AI Annotations", and stores metadata as pyarrow
    strings;
  * the prio membership columns mirror the presentation store per surface;
  * THE HASH GATE: re-synthesizing yields an identical var_schema hash
    (deterministic — no per-load drift), and the presentation-store prios are
    never hash-affecting (no study invalidation from admin surface toggles);
  * validate_contract rejects an invalid role/scale.

No Gemini API calls.

Usage:
    python tests/unit/test_contract_variable_metadata.py
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import pytest

import fyp.annotation_contract as ac
import fyp.annotation_versioning as av
import fyp.recode_variables as rv
from fyp import var_presentation as vp
from fyp.annotation_schema import flatten_structured
from fyp.fyp_config import (
    VAR_SCHEMA_COLUMNS,
    _apply_contract_variable_metadata,
    fyp_cf,
    load_var_schema,
)

META_COLS = ("role", "scale", "display_name", "description")
# Contract-owned columns whose final name differs from the contract field name.
EXPECTED_RENAMED = {"transcript_no_repetitions", "speech_vs_music",
                    "background_music", "notable_sounds",
                    "faces_gender", "faces_age_estimate", "faces_ethnicity"}


def _skeleton() -> pd.DataFrame:
    """The empty typed frame load_var_schema starts the synthesis from."""
    return pd.DataFrame(
        {c: pd.Series(dtype="string[pyarrow]") for c in VAR_SCHEMA_COLUMNS}
    )


@contextlib.contextmanager
def _swapped(frame: pd.DataFrame):
    saved = fyp_cf.get("var_schema")
    fyp_cf["var_schema"] = frame
    try:
        yield
    finally:
        fyp_cf["var_schema"] = saved


def _overlaid_skeleton() -> pd.DataFrame:
    """The annotation overlay applied to an empty skeleton (row injection path)."""
    with _swapped(_skeleton()):
        _apply_contract_variable_metadata(fyp_cf)
        return fyp_cf["var_schema"]


def _legacy_meta() -> dict:
    """Registry-snapshot metadata for fields the CURRENT contract no longer owns."""
    current = ac.contract_column_metadata(ac.load_contract())
    return {k: v for k, v in av.union_field_metadata().items() if k not in current}


def test_overlay_injects_contract_rows_on_skeleton() -> None:
    meta = ac.contract_column_metadata(ac.load_contract())
    frame = _overlaid_skeleton()
    for col, m in meta.items():
        row = frame.loc[frame["variable_name"] == col]
        assert len(row) == 1, f"{col}: expected exactly one injected row, got {len(row)}"
        for k in META_COLS:
            got = str(row[k].iloc[0])
            assert got == str(m[k]), f"{col}.{k}: {got!r} != contract {m[k]!r}"
        assert str(row["section"].iloc[0]) == "AI Annotations", (
            f"{col} must be grouped under AI Annotations"
        )


@pytest.mark.requires_data  # reads live recoded/registry files from local_data
def test_overlay_injects_legacy_registry_rows() -> None:
    legacy = _legacy_meta()
    assert legacy, (
        "no legacy annotation fields in the registry union — expected superseded "
        "fields (e.g. trend / australian_relevance); is annotation_versions.json missing?"
    )
    frame = _overlaid_skeleton()
    for col, m in legacy.items():
        row = frame.loc[frame["variable_name"] == col]
        assert len(row) == 1, f"legacy {col}: expected exactly one injected row"
        for k in ("role", "scale", "display_name"):
            if m.get(k) is not None:
                got = str(row[k].iloc[0])
                assert got == str(m[k]), f"legacy {col}.{k}: {got!r} != snapshot {m[k]!r}"
        assert str(row["section"].iloc[0]) == "AI Annotations", (
            f"legacy {col} must be grouped under AI Annotations"
        )


def test_current_contract_wins_over_legacy_snapshots() -> None:
    meta = ac.contract_column_metadata(ac.load_contract())
    overlap = set(meta) & set(av.union_field_metadata())
    frame = _overlaid_skeleton().set_index("variable_name")
    for col in overlap:
        for k in META_COLS:
            got = str(frame.at[col, k])
            assert got == str(meta[col][k]), (
                f"{col}.{k}: {got!r} != current contract {meta[col][k]!r} — "
                "a registry snapshot must never shadow the live contract"
            )


def test_overlay_is_idempotent() -> None:
    once = _overlaid_skeleton()
    with _swapped(once.copy()):
        _apply_contract_variable_metadata(fyp_cf)
        twice = fyp_cf["var_schema"]
    key = lambda df: df.sort_values("variable_name").reset_index(drop=True).astype(str).to_csv(index=False)
    assert len(twice) == len(once), "second overlay pass injected duplicate rows"
    assert key(twice) == key(once), "second overlay pass changed the frame"


def test_column_mapping_matches_flattener() -> None:
    contract = ac.load_contract()
    keys = set(ac.contract_column_metadata(contract).keys())
    assert len(keys) == 27, f"expected 27 contract-owned columns, got {len(keys)}"
    # The renamed/exploded columns must be present (and the raw field names absent).
    assert EXPECTED_RENAMED <= keys, f"missing renamed columns: {EXPECTED_RENAMED - keys}"
    for raw_name in ("transcript", "faces", "audio_summary",
                     "audio_summary_speech_vs_music"):
        assert raw_name not in keys, f"{raw_name} should not be a final column"
    # Cross-check the object explode/strip against the real flattener + rename chain.
    response = {
        "faces": [{"gender": "Female", "age_estimate": 30, "ethnicity": "Caucasian"}],
        "audio_summary": {"speech_vs_music": 50, "background_music": "upbeat",
                          "notable_sounds": ["siren"]},
    }
    flat = flatten_structured(response)
    renamed = set(rv.rename_columns(pd.DataFrame([flat])).columns)
    for col in ("faces_gender", "faces_age_estimate", "faces_ethnicity",
                "speech_vs_music", "background_music", "notable_sounds"):
        assert col in renamed, f"flattener did not emit {col}"
        assert col in keys, f"{col} emitted by flattener but not in metadata"


def test_synthesized_schema_carries_owned_rows() -> None:
    vs = fyp_cf["var_schema"]
    meta = ac.contract_column_metadata(ac.load_contract())
    owned = {**_legacy_meta(), **meta}
    names = vs["variable_name"].astype(str)
    for col in owned:
        assert int((names == col).sum()) == 1, f"{col}: not exactly once in synthesized schema"
    gemini_rows = vs.loc[names.isin(owned)]
    assert (gemini_rows["section"].astype(str) == "AI Annotations").all(), (
        "every annotation-owned row must sit under AI Annotations"
    )
    # A non-Gemini row keeps its own contract's section.
    non = vs.loc[names == "activity_type", "section"]
    assert len(non) == 1 and str(non.iloc[0]) != "AI Annotations", (
        "non-Gemini row must keep its contract section"
    )
    # load_var_schema re-coerces the injected metadata columns to pyarrow strings.
    for col in ("variable_name", "source", "role", "scale", "display_name",
                "description", "section"):
        assert str(vs[col].dtype) == "string", f"{col} dtype degraded to {vs[col].dtype}"


def test_prio_columns_mirror_presentation_store() -> None:
    vs = fyp_cf["var_schema"]
    presentation = vp.load_presentation() or vp.empty_presentation()
    names = set(vs["variable_name"].astype(str))
    for surface, col in vp.SURFACE_TO_PRIO_COLUMN.items():
        members = set(presentation.get("surfaces", {}).get(surface, []) or [])
        on = set(vs.loc[vs[col].astype("string") == "1", "variable_name"].astype(str))
        assert on == (members & names), (
            f"{surface}: prio column {col} does not mirror the presentation store "
            f"(extra: {on - members}, missing: {(members & names) - on})"
        )
        off = vs.loc[~vs["variable_name"].astype(str).isin(members), col]
        assert off.isna().all(), f"{surface}: non-members must have a blank {col}"


def test_hash_deterministic_and_prios_never_hash() -> None:
    """THE GATE: re-synthesis is hash-stable; presentation prios never invalidate."""
    hash_live = rv.compute_var_schema_hash()
    load_var_schema(fyp_cf)  # full re-synthesis from contracts + registries + store
    assert rv.compute_var_schema_hash() == hash_live, (
        "re-synthesizing the schema changed the var_schema hash -> per-load drift "
        "would invalidate every study cache"
    )
    flipped = fyp_cf["var_schema"].copy()
    for col in vp.SURFACE_TO_PRIO_COLUMN.values():
        flipped[col] = pd.Series(["1"] * len(flipped), dtype="string[pyarrow]",
                                 index=flipped.index)
    with _swapped(flipped):
        hash_flipped = rv.compute_var_schema_hash()
    assert hash_flipped == hash_live, (
        "toggling presentation prios changed the var_schema hash -> an admin "
        "surface checkbox would invalidate study caches"
    )


def test_validate_contract_rejects_bad_role_scale() -> None:
    contract = ac.load_contract()
    contract["fields"][0]["role"] = "bogus_role"
    contract["fields"][0]["scale"] = "bogus_scale"
    errors = ac.validate_contract(contract)
    assert any("invalid role" in e for e in errors), f"no role error: {errors}"
    assert any("invalid scale" in e for e in errors), f"no scale error: {errors}"


def test_list_scale_requires_a_multi_valued_response_schema() -> None:
    """A ``list`` scale must mean the response schema really yields many values.

    Regression guard. ``audio_summary.background_music`` was declared
    ``scale = "list"`` while its spec carries no ``list:`` prefix, so Gemini
    returned ONE prose string. The ``list`` scale then ran the list cleaner over
    that sentence — stripping punctuation and digits ("a short, upbeat beat (2s)"
    → "a short upbeat beat s") and wrapping it in a one-element list. Downstream,
    set-overlap metrics on such a column degenerate to exact-phrase matching.

    A field yields many values iff its own spec is ``list: ...`` / ``array = true``,
    OR its parent object field is ``array = true`` (one value per array element,
    pipe-joined by the flattener).

    Only this direction is invariant. The converse does NOT hold: a multi-valued
    sub-key may legitimately be aggregated to a scalar — ``faces.age_estimate``
    is ``scale = "numeric"`` under an ``array = true`` parent and recodes
    ``"60 | 55 | 50"`` to its mean, 55.
    """
    contract = ac.load_contract()
    offenders: list[str] = []

    def _check(column: str, scale: str | None, multi: bool) -> None:
        if scale == "list" and not multi:
            offenders.append(
                f"{column}: scale='list' but the response schema yields a single value"
            )

    for field in contract.get("fields", []):
        name = field["name"]
        if field.get("type") == "object":
            parent_is_array = bool(field.get("array"))
            for key, spec in (field.get("keys") or {}).items():
                raw = spec if isinstance(spec, str) else (spec or {}).get("spec", "")
                scale = (spec or {}).get("scale") if isinstance(spec, dict) else None
                multi = parent_is_array or str(raw).startswith("list:")
                _check(ac.contract_output_column(name, key), scale, multi)
        else:
            _check(ac.contract_output_column(name), field.get("scale"),
                   bool(field.get("array")))

    assert not offenders, "scale/response-schema mismatch:\n  " + "\n  ".join(offenders)


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception:
            failures += 1
            import traceback

            print(f"ERROR {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
