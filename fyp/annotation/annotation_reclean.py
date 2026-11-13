"""Re-clean chosen variables of stored annotations with today's cleaning code.

The model's raw answer to every video is kept. The step that turns an answer
into columns (flatten, recode, label clean-up) is applied once, when the batch
is refined, and its result is stored. When that step had an error, the error
stays in the stored rows until the answers are cleaned again. This module does
that, in two modes:

* a **dry run** refines each raw file in memory, compares the result with the
  stored refined rows and reports, per variable, how many stored values would
  differ, with a cross-tab of ``content_category`` (old first label against
  new first label). It writes nothing but its own report.
* a **real run** replaces, in the stored refined files, the values of the
  variables it is told to and nothing else, after keeping a copy of each
  original file. When it has finished it leaves a marker that the next
  consolidation picks up: the annotation dataset is rebuilt from the refined
  files through the normal path and the videos whose values changed are
  reported as changed, so the preferred view and the studies follow.

Why only named variables: today's cleaning differs from the cleaning of a
year ago in more than its errors. It no longer produces some old variables,
writes lists and missing values differently, and reads some old answers
worse than the code of their time did. Replacing whole rows would trade one
known error for many unexamined changes. The dry run shows every variable so
that the choice of what to replace is an informed one.

A re-clean is not a re-annotation. Rows are matched by video id and every
column that is not named stays as stored, ``annotation_version``,
``inference_ts`` and ``source_platform`` included. A row that today's
cleaning can make nothing of keeps its stored values. A file is left alone
(and listed in the report) when its stored rows and its raw answers do not
cover the same videos.

What it cannot repair: stored rows whose raw file no longer exists
(:func:`files_without_raw`). Their answers are gone, so they stay as cleaned.
"""

import os
from collections import Counter

import numpy as np
import pandas as pd

import fyp.annotation.annotation_versioning as annotation_versioning
import fyp.core.data_io as data_io
from fyp.core.logging_setup import get_logger
from fyp.core.runtime import cf as _cf
from fyp.core.runtime import label, utc_now_iso

logger = get_logger(__name__)

STATE_LOCATION = "recoded"
STATE_FILENAME = "annotation_reclean_state.json"
REPORT_FILENAME = "annotation_reclean_report.json"
# Written when a real run finishes; consumed (and removed) by the next
# consolidation — see annotation_refinement.consolidate_and_save_refined_annotations.
PENDING_FILENAME = "annotation_reclean_pending.json"
BACKUP_LOCATION = "machine_annotations_refined_before_reclean"

CROSSTAB_VARIABLE = "content_category"
# A row's identity and bookkeeping: never replaced, whatever is asked for.
PROTECTED_COLUMNS = (
    "annotation_version",
    "inference_ts",
    "source_platform",
    "annotated_ok",
    "annotated_fail",
)
_NOT_VARIABLES = {"item_id", *PROTECTED_COLUMNS}
_NO_VALUE = "<NA>"


def _prefix() -> str:
    return label("MACHINE_ANNOTATIONS_LABEL")


def ensure_backup_location() -> None:
    """Register the folder that keeps the refined files as they were (idempotent)."""
    data_io.register_location(
        BACKUP_LOCATION, os.path.join(_cf()["paths"]["machine_annotations"], BACKUP_LOCATION)
    )


def _listed(location: str, suffix: str) -> set[str]:
    return {
        fn[: -len(suffix)]
        for fn in data_io.listdir(storage_location=location)
        if fn.startswith(_prefix()) and fn.endswith(suffix)
    }


def files_without_raw() -> list[str]:
    """Stored refined files whose raw answers are gone: they cannot be re-cleaned."""
    refined = _listed("machine_annotations_refined", ".parquet")
    raw = _listed("machine_annotations_raw", ".json")
    return sorted(f"{name}.parquet" for name in refined - raw)


def _holds_legacy_rows(parquet_filename: str) -> bool:
    """Whether a stored refined file has rows of the legacy (pre-versioning) version."""
    try:
        frame = data_io.load_parquet_selective(
            storage_location="machine_annotations_refined",
            filename=parquet_filename,
            columns=["annotation_version"],
        )
    except Exception:
        return True  # unreadable here: let the comparison look at it and say why
    if frame is None or "annotation_version" not in frame.columns:
        return True
    versions = frame["annotation_version"].fillna(annotation_versioning.LEGACY_VERSION)
    return bool((versions == annotation_versioning.LEGACY_VERSION).any())


def candidate_files(only_legacy: bool = True) -> list[str]:
    """Raw files that can be re-cleaned, oldest first.

    A candidate has both its raw answers and a stored refined file (a raw file
    not refined yet is the ordinary refinement's business).

    Args:
        only_legacy: keep only files holding annotations of the legacy
            version, the ones cleaned by the oldest code.

    Returns:
        Raw filenames (``….json``), sorted.
    """
    both = _listed("machine_annotations_refined", ".parquet") & _listed(
        "machine_annotations_raw", ".json"
    )
    names = sorted(both)
    if only_legacy:
        names = [name for name in names if _holds_legacy_rows(f"{name}.parquet")]
    return [f"{name}.json" for name in names]


def _cell(value) -> str:
    """One stored value as comparable text (lists keep their order)."""
    if isinstance(value, (list, tuple, np.ndarray)):
        return "[" + ", ".join(_cell(v) for v in value) + "]"
    try:
        if pd.isna(value):
            return _NO_VALUE
    except (TypeError, ValueError):
        pass
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.6f}"
    if isinstance(value, (bool, np.bool_)):
        return str(bool(value))
    if isinstance(value, (int, np.integer)):
        return f"{float(value):.6f}"
    return str(value)


def _first_label(value) -> str:
    """The first label of a (list) category value, for the cross-tab."""
    if isinstance(value, (list, tuple, np.ndarray)):
        return str(value[0]) if len(value) else _NO_VALUE
    try:
        if pd.isna(value):
            return _NO_VALUE
    except (TypeError, ValueError):
        pass
    return str(value)


def _text_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(_NO_VALUE, index=frame.index, dtype=object)
    return frame[column].map(_cell)


def _is_ok(frame: pd.DataFrame) -> pd.Series:
    if "annotated_ok" not in frame.columns:
        return pd.Series(True, index=frame.index)
    return frame["annotated_ok"].fillna(False).astype(bool)


def empty_report() -> dict:
    """A report with nothing in it yet (see :func:`merge_reports` for the fields)."""
    return {
        "files": 0,
        "files_changed": 0,
        "files_blocked": {},
        "rows": 0,
        "rows_changed": 0,
        "rows_cleaning_fails_on": 0,
        "values_changed": {},
        "columns_only_stored": {},
        "columns_only_new": {},
        "crosstab": {},
    }


def merge_reports(total: dict, part: dict) -> dict:
    """Add one file's report to the running total (in place); return the total.

    Fields: ``files`` / ``rows`` are what was looked at; ``files_blocked``
    maps a file left alone to the reason; ``values_changed`` maps EVERY
    variable to the number of stored values today's cleaning gives
    differently; ``rows_changed`` / ``files_changed`` count rows and files
    that differ in the variables the run replaces (in any variable, when the
    run names none); ``rows_cleaning_fails_on`` counts successfully
    annotated stored rows that today's cleaning cannot read, which keep their
    stored values; ``columns_only_stored`` / ``columns_only_new`` count the
    files in which a variable exists on one side only; ``crosstab`` is
    ``{old first label: {new first label: rows}}`` for ``content_category``.
    """
    for key in ("files", "files_changed", "rows", "rows_changed", "rows_cleaning_fails_on"):
        total[key] += part.get(key, 0)
    total["files_blocked"].update(part.get("files_blocked", {}))
    for key in ("values_changed", "columns_only_stored", "columns_only_new"):
        for name, n in part.get(key, {}).items():
            total[key][name] = total[key].get(name, 0) + n
    for old, row in part.get("crosstab", {}).items():
        target = total["crosstab"].setdefault(old, {})
        for new, n in row.items():
            target[new] = target.get(new, 0) + n
    return total


def _plain(value):
    """A stored cell as a plain Python value (arrays become lists, NA becomes None)."""
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def compare_file(
    raw_filename: str, variables: list[str] | None = None
) -> tuple[pd.DataFrame | None, dict, list[str]]:
    """Refine one raw file in memory and compare it with its stored refined rows.

    Nothing is written. Rows are matched by video id.

    Args:
        raw_filename: the raw file (``….json``).
        variables: the variables a real run would replace. ``None`` compares
            only (the report then counts a row as changed when any variable
            differs).

    Returns:
        ``(rows_to_store, report, changed_item_ids)``. ``rows_to_store`` is the
        stored frame with the named variables replaced, or ``None`` when no
        variables are named or the file is blocked (the report's
        ``files_blocked`` says why).
    """
    from fyp.annotation.annotation_refinement import refine_one_raw_annotation_batch

    parquet_filename = raw_filename.replace(".json", ".parquet")
    report = empty_report()
    report["files"] = 1

    def _blocked(reason: str) -> tuple[None, dict, list[str]]:
        report["files_blocked"][raw_filename] = reason
        return None, report, []

    stored = data_io.load_parquet(
        storage_location="machine_annotations_refined", filename=parquet_filename
    )
    report["rows"] = int(len(stored))
    raw = data_io.load_json(storage_location="machine_annotations_raw", filename=raw_filename)
    if not isinstance(raw, dict) or not raw:
        return _blocked("the raw file holds no answers")
    try:
        new = refine_one_raw_annotation_batch(
            raw_outputs_from_machine=raw, raw_json_filename=raw_filename, save=False
        )
    except Exception as exc:
        return _blocked(f"refinement failed: {type(exc).__name__}: {exc}")
    if new is None:
        return _blocked("refinement found no usable answer")

    stored = stored.reset_index(drop=True)
    stored_ids = stored["item_id"].astype(str)
    new = new.assign(item_id=new["item_id"].astype(str)).reset_index(drop=True)
    if stored_ids.duplicated().any() or new["item_id"].duplicated().any():
        return _blocked("a video appears twice in the file; rows cannot be matched one to one")
    if set(stored_ids) != set(new["item_id"]):
        lost = len(set(stored_ids) - set(new["item_id"]))
        gained = len(set(new["item_id"]) - set(stored_ids))
        return _blocked(
            f"stored rows and raw answers cover different videos: {lost} only stored, "
            f"{gained} only in the raw file"
        )
    # Same row order as the stored file, so the frames compare positionally.
    new = new.set_index("item_id").loc[stored_ids].reset_index()

    # A row today's cleaning can make nothing of keeps its stored values.
    readable = _is_ok(new).to_numpy()
    report["rows_cleaning_fails_on"] = int((_is_ok(stored).to_numpy() & ~readable).sum())

    in_stored = set(stored.columns) - _NOT_VARIABLES
    in_new = set(new.columns) - _NOT_VARIABLES
    report["columns_only_stored"] = dict.fromkeys(sorted(in_stored - in_new), 1)
    report["columns_only_new"] = dict.fromkeys(sorted(in_new - in_stored), 1)
    differs_by_variable: dict[str, np.ndarray] = {}
    for column in sorted(in_stored | in_new):
        differs = (_text_column(stored, column) != _text_column(new, column)).to_numpy()
        differs &= readable
        if differs.any():
            report["values_changed"][column] = int(differs.sum())
            differs_by_variable[column] = differs

    wanted = [v for v in (variables or []) if v not in _NOT_VARIABLES]
    counted = wanted if variables is not None else list(differs_by_variable)
    row_changed = np.zeros(len(stored), dtype=bool)
    for column in counted:
        if column in differs_by_variable:
            row_changed |= differs_by_variable[column]
    report["rows_changed"] = int(row_changed.sum())
    report["files_changed"] = int(row_changed.any())

    if CROSSTAB_VARIABLE in stored.columns and CROSSTAB_VARIABLE in new.columns:
        old_first = stored[CROSSTAB_VARIABLE].map(_first_label)
        new_first = new[CROSSTAB_VARIABLE].map(_first_label).where(readable, old_first)
        for (old, fresh), n in Counter(zip(old_first, new_first, strict=True)).items():
            report["crosstab"].setdefault(old, {})[fresh] = int(n)

    changed_ids = stored_ids[row_changed].tolist()
    if not wanted:
        return None, report, changed_ids
    missing = [v for v in wanted if v not in new.columns]
    if missing:
        return _blocked(f"today's cleaning does not produce: {', '.join(missing)}")

    result = stored.copy()
    for column in wanted:
        fresh = [_plain(v) for v in new[column]]
        kept = (
            [_plain(v) for v in stored[column]] if column in stored.columns else [None] * len(new)
        )
        values = [f if ok else k for f, k, ok in zip(fresh, kept, readable, strict=True)]
        result[column] = pd.array(values, dtype=new[column].dtype)
    return result, report, changed_ids


def reclean_file(
    raw_filename: str, dry_run: bool = True, variables: list[str] | None = None
) -> tuple[dict, list[str]]:
    """Compare one file and, in a real run, replace the named variables in it.

    A real run first copies the stored refined file to the backup folder
    (once: an existing copy is the original and is kept), then replaces it.
    A file in which the named variables would not change is not rewritten.

    Returns:
        ``(report, changed_item_ids)``.
    """
    rows_to_store, report, changed = compare_file(raw_filename, variables)
    if dry_run or rows_to_store is None or not changed:
        return report, changed
    parquet_filename = raw_filename.replace(".json", ".parquet")
    ensure_backup_location()
    if not data_io.exists(storage_location=BACKUP_LOCATION, filename=parquet_filename):
        original = data_io.load_parquet(
            storage_location="machine_annotations_refined", filename=parquet_filename
        )
        data_io.save_parquet(
            df=original, storage_location=BACKUP_LOCATION, filename=parquet_filename
        )
    data_io.save_parquet(
        df=rows_to_store, storage_location="machine_annotations_refined", filename=parquet_filename
    )
    return report, changed


# ---------------------------------------------------------------------------
# A run over many files, resumable between background tasks
# ---------------------------------------------------------------------------


def load_state() -> dict | None:
    """The run in progress (or the last one), or ``None``."""
    state = data_io.load_json_optional(storage_location=STATE_LOCATION, filename=STATE_FILENAME)
    return state if isinstance(state, dict) else None


def load_report() -> dict | None:
    """The report of the last finished run, or ``None``."""
    report = data_io.load_json_optional(storage_location=STATE_LOCATION, filename=REPORT_FILENAME)
    return report if isinstance(report, dict) else None


def start_run(
    dry_run: bool = True,
    only_legacy: bool = True,
    started_by: str = "",
    variables: list[str] | None = None,
) -> dict:
    """Plan a run: list its files and store the starting state.

    Args:
        dry_run: compare and report only.
        only_legacy: restrict to files holding legacy-version annotations.
        started_by: audit actor.
        variables: the variables to replace. Required for a real run; in a
            dry run it only decides which rows count as changed.

    Raises:
        ValueError: a real run without variables (or naming a protected
            column), or an earlier real run's changes still waiting for a
            consolidation (run that first, so its changed videos are not lost).
    """
    variables = [str(v).strip() for v in (variables or []) if str(v).strip()] or None
    if not dry_run:
        if not variables:
            raise ValueError("a real run must name the variables to replace")
        protected = sorted(set(variables) & _NOT_VARIABLES)
        if protected:
            raise ValueError(f"never replaced: {', '.join(protected)}")
    if not dry_run and load_pending() is not None:
        raise ValueError("an earlier re-clean still waits for a consolidation; consolidate first")
    # A real run that died before it could leave its marker has already
    # replaced some files. Run again, those files show nothing left to change,
    # so the videos it changed are carried over or they would never be
    # reported to a consolidation.
    carried: list[str] = []
    previous = load_state()
    if (
        not dry_run
        and previous
        and not previous.get("dry_run")
        and previous.get("status") == "running"
    ):
        carried = list(previous.get("changed_item_ids") or [])
    state = {
        "started_at": utc_now_iso(timespec="seconds"),
        "started_by": started_by,
        "dry_run": bool(dry_run),
        "only_legacy": bool(only_legacy),
        "variables": variables,
        "files": candidate_files(only_legacy=only_legacy),
        "next_index": 0,
        "status": "running",
        "report": empty_report(),
        "changed_item_ids": carried,
        "files_without_raw": files_without_raw(),
    }
    _save_state(state)
    return state


def _save_state(state: dict) -> None:
    data_io.save_json(data=state, storage_location=STATE_LOCATION, filename=STATE_FILENAME)


def continue_run(state: dict, should_pause=None, progress_cb=None) -> dict:
    """Work through the run's remaining files; return the updated state.

    The state is saved after every file, so a run stopped at any point
    continues with the next file. Re-cleaning a file twice is harmless: the
    second pass finds nothing to change.

    Args:
        state: from :func:`start_run` or :func:`load_state`.
        should_pause: zero-arg callable asked after each file; True stops here
            with ``status`` still ``running``.
        progress_cb: ``(done, total, filename)`` after each file.
    """
    files = state["files"]
    changed = set(state.get("changed_item_ids") or [])
    while state["next_index"] < len(files):
        filename = files[state["next_index"]]
        try:
            report, ids = reclean_file(
                filename, dry_run=state["dry_run"], variables=state.get("variables")
            )
        except Exception as exc:  # one unreadable file must not end the run
            report = empty_report()
            report["files"] = 1
            report["files_blocked"][filename] = f"{type(exc).__name__}: {exc}"
            ids = []
        merge_reports(state["report"], report)
        changed.update(ids)
        state["changed_item_ids"] = sorted(changed)
        state["next_index"] += 1
        _save_state(state)
        if progress_cb:
            progress_cb(state["next_index"], len(files), filename)
        if should_pause and should_pause() and state["next_index"] < len(files):
            return state
    return finish_run(state)


def finish_run(state: dict, status: str = "complete") -> dict:
    """Close a run: store its report and, for a real run, the consolidation marker.

    A cancelled real run is closed the same way, so the files it did replace
    reach the next consolidation.
    """
    state["status"] = status
    state["finished_at"] = utc_now_iso(timespec="seconds")
    report = {
        "started_at": state["started_at"],
        "finished_at": state["finished_at"],
        "started_by": state.get("started_by", ""),
        "status": status,
        "dry_run": state["dry_run"],
        "only_legacy": state["only_legacy"],
        "variables": state.get("variables"),
        "n_files": len(state["files"]),
        "n_files_done": state["next_index"],
        "n_videos_changed": len(state["changed_item_ids"]),
        "files_without_raw": state.get("files_without_raw", []),
        **state["report"],
    }
    data_io.save_json(data=report, storage_location=STATE_LOCATION, filename=REPORT_FILENAME)
    if not state["dry_run"] and state["changed_item_ids"]:
        data_io.save_json(
            data={
                "finished_at": state["finished_at"],
                "variables": state.get("variables"),
                "changed_item_ids": state["changed_item_ids"],
            },
            storage_location=STATE_LOCATION,
            filename=PENDING_FILENAME,
        )
    _save_state(state)
    return state


def load_pending() -> dict | None:
    """The marker a finished real run left for the next consolidation, or ``None``."""
    pending = data_io.load_json_optional(storage_location=STATE_LOCATION, filename=PENDING_FILENAME)
    return pending if isinstance(pending, dict) else None


def clear_pending() -> None:
    """Remove the marker once a consolidation has taken the changes in."""
    if data_io.exists(storage_location=STATE_LOCATION, filename=PENDING_FILENAME):
        data_io.remove(storage_location=STATE_LOCATION, filename=PENDING_FILENAME)
