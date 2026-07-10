"""Human input for annotation A/B test runs (coding tasks + ICR metrics).

Extends the :mod:`fyp.ab_eval` harness with run-scoped *human tasks*: an admin
picks a finished run, a subset of its compared variables and a set of coders;
each coder then watches the run's videos FULLY BLIND (machine values are never
sent to the coder endpoints) and fills in values for the selected variables.
Submitted codings are compared against every machine arm — and against each
other when several coders submit — with the same scale-aware metrics the
machine-vs-machine report uses, plus Cohen's kappa for enum fields.

Storage (all in the isolated ``ab_eval`` location, next to the run artifacts):

* ``human_tasks_index.json``                 — global task index (coder lookup).
* ``runs/{run_id}/human/task_{type}.json``   — the task definition.
* ``runs/{run_id}/human/{type}_{slug}.json`` — one file per coder (their own
  responses only, so concurrent coders never write the same file).
* ``runs/{run_id}/human/results_{type}.json``— computed ICR metrics.

Cardinality: at most one task per ``(run_id, task_type)``; re-setup requires
an explicit delete. ``task_type`` is ``"coding"`` now; ``"vote"`` (per-item
blind preference votes) reuses this layout in a later phase.

METRIC RULE (inherited from ab_eval): a variable's comparison kind comes from
the scale snapshotted into the task at creation, never from answer length.
"""

import hashlib
import re
import threading
from datetime import UTC, datetime

import numpy as np
import pandas as pd

import fyp.ab_eval as ab
import fyp.data_io as data_io
from fyp import annotation_contract as ac
from fyp.fyp_config import fyp_cf

TASK_TYPES = ("coding", "vote")
TASKS_INDEX_FILENAME = "human_tasks_index.json"

# Minimum paired observations before a Cohen's kappa is worth reporting.
_MIN_KAPPA_N = 5

# Guards the read-modify-write of the shared task index / task definition
# (same in-process laxity as ab_eval's runs index — prod is 1 worker).
_INDEX_LOCK = threading.Lock()

_ENUM_SPEC_RE = re.compile(r"enum:([A-Za-z0-9_]+)")




def _now_iso() -> str:
    """Return the current UTC time as a seconds-precision ISO string."""
    return datetime.now(UTC).isoformat(timespec="seconds")




def coder_slug(username: str) -> str:
    """Return a filename-safe, collision-safe slug for a coder's username.

    Usernames are emails; the sanitized prefix keeps files recognisable and the
    hash suffix disambiguates two emails that sanitize identically.

    Args:
        username: the coder's username (email).

    Returns:
        ``<sanitized>_<6-hex-hash>``.
    """
    sanitized = re.sub(r"[^a-z0-9_\-]+", "_", str(username).strip().lower())[:40]
    digest = hashlib.sha256(str(username).encode("utf-8")).hexdigest()[:6]
    return f"{sanitized}_{digest}"




def _human_file(run_id: str, name: str) -> str:
    """Return the storage filename for one human-task artifact of a run."""
    return f"runs/{run_id}/human/{name}"




def _task_filename(task_type: str) -> str:
    """Return the task-definition filename for a task type."""
    return f"task_{task_type}.json"




def _coder_filename(task_type: str, username: str) -> str:
    """Return a coder's response filename for a task type."""
    return f"{task_type}_{coder_slug(username)}.json"




def _results_filename(task_type: str) -> str:
    """Return the computed-results filename for a task type."""
    return f"results_{task_type}.json"




def _check_task_type(task_type: str) -> None:
    """Raise ``ValueError`` on an unknown task type."""
    if task_type not in TASK_TYPES:
        raise ValueError(f"unknown task type: {task_type!r}")




def _contract_enum_values(contract: dict) -> dict[str, list[str]]:
    """Return ``{output_column: [values]}`` for every enum-backed contract field.

    Covers scalar fields (``enum = "name"``) and object sub-keys whose ``spec``
    references an enum (``spec = "enum:name"``), keyed on the final post-recode
    column name.
    """
    out: dict[str, list[str]] = {}
    enums = contract.get("enums", {}) or {}
    for field in contract.get("fields", []):
        name = field.get("name")
        if not name:
            continue
        if field.get("type") == "object":
            for key, spec in (field.get("keys") or {}).items():
                spec_str = spec.get("spec", "") if isinstance(spec, dict) else ""
                match = _ENUM_SPEC_RE.search(str(spec_str))
                if match and match.group(1) in enums:
                    out[ac.contract_output_column(name, key)] = ac.enum_values(
                        contract, match.group(1)
                    )
        elif field.get("enum") and field["enum"] in enums:
            out[ac.contract_output_column(name)] = ac.enum_values(contract, field["enum"])
    return out




def available_variables(run_id: str) -> list[dict]:
    """Return the variables of a finished run a human task can cover.

    The union of every compared column in the run report, each with the kind
    the machine comparison used, a display label, and (for enum/list columns)
    the accepted value list — from the live contract when it declares one,
    otherwise the values observed in the run's distribution tables.

    Args:
        run_id: the finished run's id.

    Returns:
        ``[{name, kind, label, description, values}]`` sorted by label.
    """
    run = ab.load_run(run_id)
    report = run.get("report") or {}
    comparisons = report.get("comparisons") or {}

    kinds: dict[str, str] = {}
    for comp in comparisons.values():
        for col, info in (comp.get("columns") or {}).items():
            kinds.setdefault(col, info.get("kind") or "freetext")

    # Live-contract enum values + var_schema labels; a candidate-only column
    # simply misses these and falls back to observed values / its raw name.
    contract = ac.load_contract()
    enum_map = _contract_enum_values(contract) if contract else {}
    col_meta = ac.contract_column_metadata(contract) if contract else {}
    vs = fyp_cf.get("var_schema")
    vs_meta: dict[str, dict] = {}
    if vs is not None:
        for _, row in vs.iterrows():
            vs_meta[str(row.get("variable_name"))] = {
                "label": row.get("display_name"),
                "description": row.get("description"),
            }

    distributions = report.get("distributions") or {}
    out = []
    for col, kind in kinds.items():
        meta = vs_meta.get(col) or {}
        cmeta = col_meta.get(col) or {}
        label = meta.get("label") or cmeta.get("display_name") or col
        description = meta.get("description") or cmeta.get("description") or ""
        values = enum_map.get(col)
        if values is None and kind == "enum":
            observed: set[str] = set()
            for arm_counts in (distributions.get(col, {}).get("arms") or {}).values():
                observed |= {v for v in arm_counts if not ab._is_sentinel(v)}
            values = sorted(observed) if observed else None
        out.append({
            "name": col,
            "kind": kind,
            "label": str(label) if pd.notna(label) else col,
            "description": str(description) if pd.notna(description) else "",
            "values": values,
        })
    return sorted(out, key=lambda v: v["label"].lower())




def create_task(run_id: str, task_type: str, variables: list[str],
                coders: list[str], created_by: str = "") -> dict:
    """Create a human task on a finished run.

    Snapshots the run's items (with platforms, for the video player) and each
    selected variable's spec, so the task stays self-contained even if the
    live contract or the run's eval set changes later.

    Args:
        run_id: the run to attach the task to (must be ``complete``).
        task_type: ``"coding"`` (``"vote"`` reserved for a later phase).
        variables: the compared columns coders provide input for.
        coders: usernames to invite (admins are always implicitly invited).
        created_by: audit actor.

    Returns:
        The stored task definition.

    Raises:
        ValueError: unknown type / run not complete / task exists / bad variables.
    """
    _check_task_type(task_type)
    if task_type != "coding":
        raise ValueError("only 'coding' tasks are supported in this phase")

    run = ab.load_run(run_id)
    manifest = run.get("manifest") or {}
    if manifest.get("status") != "complete":
        raise ValueError(f"run {run_id} is not complete")
    if load_task(run_id, task_type) is not None:
        raise ValueError(f"run {run_id} already has a {task_type} task — delete it first")

    catalog = {v["name"]: v for v in available_variables(run_id)}
    variables = [str(v) for v in variables]
    if not variables:
        raise ValueError("select at least one variable")
    unknown = [v for v in variables if v not in catalog]
    if unknown:
        raise ValueError(f"unknown variables: {', '.join(unknown)}")

    item_ids = [str(i) for i in manifest.get("item_ids", [])]
    if not item_ids:
        raise ValueError(f"run {run_id} has no items")
    items = [
        {"item_id": item["item_id"], "platform": item["platform"]}
        for item in ab.resolve_items(item_ids)
    ]

    now = _now_iso()
    task = {
        "run_id": run_id,
        "task_type": task_type,
        "created_at": now,
        "created_by": created_by,
        "item_ids": item_ids,
        "items": items,
        "variables": variables,
        "field_specs": {
            v: {
                "scale": _kind_to_scale(catalog[v]["kind"]),
                "kind": catalog[v]["kind"],
                "label": catalog[v]["label"],
                "description": catalog[v]["description"],
                "values": catalog[v]["values"],
            }
            for v in variables
        },
        "coders": {
            str(u): {"invited_at": now, "invited_by": created_by, "notified": False}
            for u in dict.fromkeys(str(u) for u in coders)
        },
    }
    with _INDEX_LOCK:
        data_io.save_json(data=task, storage_location=ab.LOCATION,
                          filename=_human_file(run_id, _task_filename(task_type)))
        _update_tasks_index(_index_entry(task))
    return task




def _kind_to_scale(kind: str) -> str:
    """Map a report comparison kind back onto a declared-scale string."""
    return {"numeric": "numeric", "enum": "categorical",
            "list": "list", "freetext": "text"}.get(kind, "text")




def load_task(run_id: str, task_type: str) -> dict | None:
    """Return a task definition, or ``None`` when it does not exist."""
    _check_task_type(task_type)
    ab.ensure_locations()
    filename = _human_file(run_id, _task_filename(task_type))
    try:
        if data_io.exists(storage_location=ab.LOCATION, filename=filename):
            task = data_io.load_json(storage_location=ab.LOCATION, filename=filename)
            if isinstance(task, dict):
                return task
    except Exception:
        pass
    return None




def delete_task(run_id: str, task_type: str) -> bool:
    """Delete a task, its coder files and results; drop it from the index."""
    _check_task_type(task_type)
    ab.ensure_locations()
    task = load_task(run_id, task_type)
    filenames = [_task_filename(task_type), _results_filename(task_type)]
    filenames += [
        _coder_filename(task_type, username)
        for username in (task or {}).get("coders", {})
    ]
    removed = False
    for name in filenames:
        fn = _human_file(run_id, name)
        try:
            if data_io.exists(storage_location=ab.LOCATION, filename=fn):
                data_io.remove(storage_location=ab.LOCATION, filename=fn)
                removed = True
        except Exception:
            pass
    with _INDEX_LOCK:
        index = [
            t for t in list_tasks()
            if not (t.get("run_id") == run_id and t.get("task_type") == task_type)
        ]
        data_io.save_json(data=index, storage_location=ab.LOCATION,
                          filename=TASKS_INDEX_FILENAME)
    return removed




def list_tasks() -> list[dict]:
    """Return the global task index (newest first). Never raises."""
    ab.ensure_locations()
    try:
        if data_io.exists(storage_location=ab.LOCATION, filename=TASKS_INDEX_FILENAME):
            index = data_io.load_json(storage_location=ab.LOCATION,
                                      filename=TASKS_INDEX_FILENAME)
            if isinstance(index, list):
                return sorted(index, key=lambda t: t.get("created_at") or "", reverse=True)
    except Exception:
        pass
    return []




def _index_entry(task: dict) -> dict:
    """Build a task's index entry from its definition."""
    return {
        "run_id": task["run_id"],
        "task_type": task["task_type"],
        "created_at": task.get("created_at"),
        "created_by": task.get("created_by"),
        "n_items": len(task.get("item_ids", [])),
        "n_variables": len(task.get("variables", [])),
        "coders": sorted(task.get("coders", {})),
        "submitted": sorted(task.get("_submitted", [])),
    }




def _update_tasks_index(entry: dict, submitted: list[str] | None = None) -> None:
    """Insert/replace one task's index entry (keyed by run_id + task_type).

    Callers must hold ``_INDEX_LOCK``. When ``submitted`` is None the previous
    entry's submitted list is preserved.
    """
    index = list_tasks()
    previous = next(
        (t for t in index
         if t.get("run_id") == entry["run_id"] and t.get("task_type") == entry["task_type"]),
        None,
    )
    if submitted is not None:
        entry = {**entry, "submitted": sorted(set(submitted))}
    elif previous is not None:
        entry = {**entry, "submitted": previous.get("submitted", [])}
    index = [
        t for t in index
        if not (t.get("run_id") == entry["run_id"] and t.get("task_type") == entry["task_type"])
    ]
    index.append(entry)
    data_io.save_json(data=index, storage_location=ab.LOCATION,
                      filename=TASKS_INDEX_FILENAME)




def tasks_for_user(username: str, is_admin: bool = False) -> list[dict]:
    """Return the index entries a user may work on (all of them for admins)."""
    tasks = list_tasks()
    if is_admin:
        return tasks
    return [t for t in tasks if str(username) in (t.get("coders") or [])]




def is_invited(task: dict, username: str, is_admin: bool = False) -> bool:
    """True when a user may access a task (admins always may)."""
    return bool(is_admin) or str(username) in (task.get("coders") or {})




def add_coders(run_id: str, task_type: str, usernames: list[str],
               invited_by: str = "") -> dict:
    """Invite additional coders to an existing task.

    Args:
        run_id: the task's run.
        task_type: the task's type.
        usernames: usernames to add (already-invited ones are kept as-is).
        invited_by: audit actor.

    Returns:
        The updated task definition.

    Raises:
        ValueError: when the task does not exist.
    """
    with _INDEX_LOCK:
        task = load_task(run_id, task_type)
        if task is None:
            raise ValueError(f"run {run_id} has no {task_type} task")
        now = _now_iso()
        for username in usernames:
            username = str(username)
            if username not in task["coders"]:
                task["coders"][username] = {
                    "invited_at": now, "invited_by": invited_by, "notified": False,
                }
        data_io.save_json(data=task, storage_location=ab.LOCATION,
                          filename=_human_file(run_id, _task_filename(task_type)))
        _update_tasks_index(_index_entry(task))
    return task




def load_coder_state(run_id: str, task_type: str, username: str) -> dict:
    """Return a coder's saved state for a task (a fresh one when absent)."""
    _check_task_type(task_type)
    ab.ensure_locations()
    filename = _human_file(run_id, _coder_filename(task_type, username))
    try:
        if data_io.exists(storage_location=ab.LOCATION, filename=filename):
            state = data_io.load_json(storage_location=ab.LOCATION, filename=filename)
            if isinstance(state, dict):
                return state
    except Exception:
        pass
    return {
        "username": str(username),
        "status": "in_progress",
        "started_at": None,
        "updated_at": None,
        "submitted_at": None,
        "responses": {},
    }




def _validate_values(task: dict, values: dict) -> dict:
    """Validate and coerce one item's response values against the task specs.

    Args:
        task: the task definition.
        values: ``{variable: value}`` as posted by the coder UI.

    Returns:
        The cleaned ``{variable: value}`` dict.

    Raises:
        ValueError: unknown variable or a value violating its spec.
    """
    specs = task.get("field_specs", {})
    cleaned: dict = {}
    for var, value in (values or {}).items():
        spec = specs.get(var)
        if spec is None:
            raise ValueError(f"unknown variable: {var}")
        kind = spec.get("kind")
        if value is None:
            cleaned[var] = None
        elif kind == "numeric":
            if value == "":
                cleaned[var] = None
            else:
                try:
                    cleaned[var] = float(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{var}: not a number: {value!r}") from None
        elif kind == "list":
            if not isinstance(value, list):
                raise ValueError(f"{var}: expected a list")
            cleaned[var] = [str(v).strip() for v in value if str(v).strip()]
        elif kind == "enum":
            value = str(value).strip()
            accepted = spec.get("values")
            if value and accepted and value not in accepted:
                raise ValueError(f"{var}: {value!r} is not an accepted value")
            cleaned[var] = value
        else:  # freetext
            cleaned[var] = str(value)
    return cleaned




def save_response(run_id: str, task_type: str, username: str,
                  item_id: str, values: dict) -> dict:
    """Save one coder's response for one item (autosave granularity).

    Writes only that coder's own file, so concurrent coders never collide.

    Args:
        run_id: the task's run.
        task_type: the task's type.
        username: the coder.
        item_id: the coded item (must belong to the task).
        values: ``{variable: value}`` (validated against the task specs).

    Returns:
        The updated coder state.

    Raises:
        ValueError: missing task, foreign item, invalid values, or an
            already-submitted coder.
    """
    task = load_task(run_id, task_type)
    if task is None:
        raise ValueError(f"run {run_id} has no {task_type} task")
    if str(item_id) not in {str(i) for i in task.get("item_ids", [])}:
        raise ValueError(f"item {item_id} is not part of this task")

    state = load_coder_state(run_id, task_type, username)
    if state.get("status") == "submitted":
        raise ValueError("this coding has already been submitted")

    now = _now_iso()
    state["started_at"] = state.get("started_at") or now
    state["updated_at"] = now
    state["responses"][str(item_id)] = {
        "values": _validate_values(task, values),
        "updated_at": now,
    }
    data_io.save_json(data=state, storage_location=ab.LOCATION,
                      filename=_human_file(run_id, _coder_filename(task_type, username)))
    return state




def submit(run_id: str, task_type: str, username: str) -> dict:
    """Finalize a coder's work and recompute the task's results.

    Args:
        run_id: the task's run.
        task_type: the task's type.
        username: the coder.

    Returns:
        ``{n_answered, n_items}``.

    Raises:
        ValueError: missing task or nothing answered yet.
    """
    task = load_task(run_id, task_type)
    if task is None:
        raise ValueError(f"run {run_id} has no {task_type} task")
    state = load_coder_state(run_id, task_type, username)
    if not state.get("responses"):
        raise ValueError("nothing to submit — no items answered")

    now = _now_iso()
    state["status"] = "submitted"
    state["submitted_at"] = now
    state["updated_at"] = now
    data_io.save_json(data=state, storage_location=ab.LOCATION,
                      filename=_human_file(run_id, _coder_filename(task_type, username)))

    with _INDEX_LOCK:
        index = list_tasks()
        previous = next(
            (t for t in index
             if t.get("run_id") == run_id and t.get("task_type") == task_type),
            None,
        )
        submitted = set((previous or {}).get("submitted", [])) | {str(username)}
        _update_tasks_index(_index_entry(task), submitted=sorted(submitted))

    try:
        compute_results(run_id, task_type)
    except Exception as exc:
        print(f"[human_eval] results computation failed for {run_id}/{task_type}: {exc}")

    return {"n_answered": len(state["responses"]), "n_items": len(task.get("item_ids", []))}




def _coder_frame(task: dict, state: dict) -> pd.DataFrame:
    """Build a compare-ready frame from one coder's responses.

    One row per responded item; columns ``item_id`` + the task variables.
    List values stay Python lists (``compare_arms``' Jaccard handles them),
    numeric values are floats, blanks are empty strings.
    """
    variables = task.get("variables", [])
    rows = []
    for item_id, response in (state.get("responses") or {}).items():
        values = response.get("values") or {}
        row: dict = {"item_id": str(item_id)}
        for var in variables:
            row[var] = values.get(var)
        rows.append(row)
    frame = pd.DataFrame(rows, columns=["item_id", *variables])
    frame["item_id"] = frame["item_id"].astype(str)
    return frame




def _arm_frame(run_id: str, arm: str, variables: list[str]) -> pd.DataFrame | None:
    """Load one machine arm's refined frame restricted to the task variables."""
    try:
        frame = data_io.load_parquet(
            storage_location=ab.LOCATION,
            filename=ab._run_file(run_id, f"arm_{arm}.parquet"),
        )
    except Exception:
        return None
    if frame is None or "item_id" not in frame.columns:
        return None
    columns = ["item_id"] + [v for v in variables if v in frame.columns]
    frame = frame[columns].copy()
    frame["item_id"] = frame["item_id"].astype(str)
    return frame




def _aligned_canon(df_a: pd.DataFrame, df_b: pd.DataFrame, column: str) -> tuple[list, list]:
    """Return the two frames' canonicalized value lists for one enum column.

    Aligned on the common ``item_id`` set exactly like ``compare_arms``.
    """
    a = df_a.drop_duplicates("item_id").set_index("item_id")
    b = df_b.drop_duplicates("item_id").set_index("item_id")
    common = sorted(set(a.index) & set(b.index))
    ca = a.loc[common, column].map(ab._normalize_cell).map(ab._canon)
    cb = b.loc[common, column].map(ab._normalize_cell).map(ab._canon)
    return list(ca), list(cb)




def _inject_kappa(comparison: dict, df_a: pd.DataFrame, df_b: pd.DataFrame) -> dict:
    """Add Cohen's kappa to every enum column of a ``compare_arms`` report.

    Kappa is computed over the items where *both* sides gave a real value, and
    reported as ``None`` when there are fewer than ``_MIN_KAPPA_N`` such pairs
    or fewer than two distinct labels (kappa is undefined/degenerate there —
    the plain agreement number remains the reference). The summary gains
    ``mean_enum_kappa``.
    """
    from sklearn.metrics import cohen_kappa_score

    kappas = []
    for col, info in comparison.get("columns", {}).items():
        if info.get("kind") != "enum" or col not in df_a.columns or col not in df_b.columns:
            continue
        ca, cb = _aligned_canon(df_a, df_b, col)
        pairs = [(x, y) for x, y in zip(ca, cb, strict=False) if x and y]
        kappa = None
        if len(pairs) >= _MIN_KAPPA_N:
            y_a = [p[0] for p in pairs]
            y_b = [p[1] for p in pairs]
            if len(set(y_a) | set(y_b)) >= 2:
                try:
                    value = cohen_kappa_score(y_a, y_b)
                    if pd.notna(value):
                        kappa = float(value)
                except Exception:
                    kappa = None
        info["kappa"] = kappa
        if kappa is not None:
            kappas.append(kappa)
    comparison.setdefault("summary", {})["mean_enum_kappa"] = (
        float(np.mean(kappas)) if kappas else None
    )
    return comparison




def compute_results(run_id: str, task_type: str) -> dict:
    """(Re)compute a task's ICR metrics and persist them.

    Compares every *submitted* coder against every machine arm, and every
    submitted-coder pair against each other, using ``ab_eval.compare_arms``
    with the task's snapshotted scales — then injects Cohen's kappa per enum
    column.

    Args:
        run_id: the task's run.
        task_type: the task's type.

    Returns:
        The stored results dict.

    Raises:
        ValueError: when the task does not exist.
    """
    task = load_task(run_id, task_type)
    if task is None:
        raise ValueError(f"run {run_id} has no {task_type} task")
    variables = task.get("variables", [])
    scales = {v: spec.get("scale", "text") for v, spec in task.get("field_specs", {}).items()}

    coder_frames: dict[str, pd.DataFrame] = {}
    for username in task.get("coders", {}):
        state = load_coder_state(run_id, task_type, username)
        if state.get("status") == "submitted" and state.get("responses"):
            coder_frames[username] = _coder_frame(task, state)

    manifest = (ab.load_run(run_id).get("manifest") or {})
    arm_names = [a.get("name") for a in manifest.get("arms", []) if a.get("name")]
    arm_frames: dict[str, pd.DataFrame] = {}
    for arm in arm_names:
        frame = _arm_frame(run_id, arm, variables)
        if frame is not None:
            arm_frames[arm] = frame

    human_vs_machine: dict[str, dict] = {}
    for username, cframe in coder_frames.items():
        for arm, aframe in arm_frames.items():
            comp = ab.compare_arms(cframe, aframe, scales=scales)
            human_vs_machine[f"{username}|{arm}"] = _inject_kappa(comp, cframe, aframe)

    human_vs_human: dict[str, dict] = {}
    coders = sorted(coder_frames)
    for i in range(len(coders)):
        for j in range(i + 1, len(coders)):
            key = f"{coders[i]}|{coders[j]}"
            comp = ab.compare_arms(coder_frames[coders[i]], coder_frames[coders[j]],
                                   scales=scales)
            human_vs_human[key] = _inject_kappa(
                comp, coder_frames[coders[i]], coder_frames[coders[j]]
            )

    per_coder: dict[str, dict] = {}
    for username in coders:
        own = [comp["summary"] for key, comp in human_vs_machine.items()
               if key.split("|", 1)[0] == username]
        per_coder[username] = {
            metric: _mean_of(own, metric)
            for metric in ("mean_enum_agreement", "mean_enum_agreement_filled",
                           "mean_enum_kappa", "mean_list_jaccard",
                           "mean_numeric_correlation")
        }
        per_coder[username]["n_items_coded"] = int(len(coder_frames[username]))

    results = {
        "computed_at": _now_iso(),
        "variables": variables,
        "coders": coders,
        "arms": sorted(arm_frames),
        "human_vs_machine": human_vs_machine,
        "human_vs_human": human_vs_human,
        "summary": {"per_coder": per_coder},
    }
    data_io.save_json(data=results, storage_location=ab.LOCATION,
                      filename=_human_file(run_id, _results_filename(task_type)))
    return results




def _mean_of(summaries: list[dict], metric: str) -> float | None:
    """Mean of one summary metric across comparisons, ignoring ``None``s."""
    values = [s.get(metric) for s in summaries if s.get(metric) is not None]
    return float(np.mean(values)) if values else None




def load_results(run_id: str, task_type: str) -> dict | None:
    """Return a task's stored results, or ``None`` when absent."""
    _check_task_type(task_type)
    ab.ensure_locations()
    filename = _human_file(run_id, _results_filename(task_type))
    try:
        if data_io.exists(storage_location=ab.LOCATION, filename=filename):
            results = data_io.load_json(storage_location=ab.LOCATION, filename=filename)
            if isinstance(results, dict):
                return results
    except Exception:
        pass
    return None




def coder_status(run_id: str, task_type: str, task: dict | None = None) -> dict:
    """Return per-coder progress for a task's invited coders.

    Args:
        run_id: the task's run.
        task_type: the task's type.
        task: optional pre-loaded task definition.

    Returns:
        ``{username: {status, n_answered, updated_at, submitted_at}}`` where
        status is ``invited`` / ``in_progress`` / ``submitted``.
    """
    task = task if task is not None else load_task(run_id, task_type)
    out: dict[str, dict] = {}
    for username in (task or {}).get("coders", {}):
        state = load_coder_state(run_id, task_type, username)
        if state.get("responses") or state.get("status") == "submitted":
            status = state.get("status", "in_progress")
        else:
            status = "invited"
        out[username] = {
            "status": status,
            "n_answered": len(state.get("responses") or {}),
            "updated_at": state.get("updated_at"),
            "submitted_at": state.get("submitted_at"),
        }
    return out




def load_human(run_id: str) -> dict | None:
    """Return the run's human-input block for the run-report endpoint.

    ``None`` when the run has no human tasks; otherwise one entry per task
    type with the task summary, derived per-coder status, and the computed
    results (which may be ``None`` while nobody has submitted).
    """
    out: dict = {}
    for task_type in TASK_TYPES:
        task = load_task(run_id, task_type)
        if task is None:
            continue
        out[task_type] = {
            "created_at": task.get("created_at"),
            "created_by": task.get("created_by"),
            "n_items": len(task.get("item_ids", [])),
            "variables": task.get("variables", []),
            "coder_status": coder_status(run_id, task_type, task=task),
            "results": load_results(run_id, task_type),
        }
    return out or None
