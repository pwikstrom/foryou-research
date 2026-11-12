"""Human input for annotation A/B test runs (coding tasks + ICR metrics).

Extends the :mod:`fyp.annotation.ab_eval` harness with run-scoped *human tasks*: an admin
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
an explicit delete. Two task types share the layout: ``"coding"`` (blind
coding, above) and ``"vote"`` — per-item blind preference votes, where the
coder sees each arm's annotation values as anonymous options in a per-(item,
coder) randomized order (a pure hash of the task's ``order_seed``) and picks
the best one or a tie; the server resolves option letters back to arm names
before storing, and results are per-arm win rates plus a two-arm sign test.

METRIC RULE (inherited from ab_eval): a variable's comparison kind comes from
the scale snapshotted into the task at creation, never from answer length.
"""

import csv
import hashlib
import io
import random
import re
import secrets
import threading
from collections import Counter
from functools import partial

import numpy as np
import pandas as pd

import fyp.annotation.ab_eval as ab
import fyp.core.data_io as data_io
from fyp.annotation import annotation_contract as ac
from fyp.core.logging_setup import get_logger
from fyp.core.runtime import cf as _cf
from fyp.core.runtime import utc_now_iso

logger = get_logger(__name__)


TASK_TYPES = ("coding", "vote", "rating")
TASKS_INDEX_FILENAME = "human_tasks_index.json"

# Per-item free-text coder notes are capped to keep the JSON files small.
MAX_NOTE_CHARS = 2000

# Minimum paired observations before a Cohen's kappa is worth reporting.
_MIN_KAPPA_N = 5

# Guards the read-modify-write of the shared task index / task definition
# (same in-process laxity as ab_eval's runs index — prod is 1 worker).
_INDEX_LOCK = threading.Lock()

_ENUM_SPEC_RE = re.compile(r"enum:([A-Za-z0-9_]+)")

# A coder's explicit "can't tell", stored as written so the export shows it.
CANT_TELL = "can't tell"

# How the coder form words a codebook's "-" answer. "-" means different things
# in different fields; a closed list not named here reads "Doesn't apply". An
# open category (no codebook list) offers "-" only when named here, i.e. when
# its codebook instruction defines it.
_DASH_LABELS = {
    "main_gender": "No person in the video",
    "main_ethnicity": "No person in the video",
    "faces_gender": "Can't tell",
    "spoken_language": "No speech or singing",
    "primary_country": "No clear country connection",
}

# Codebook answers that already mean "can't tell": a field offering one gets
# no extra "Can't tell" option.
_CANT_TELL_WORDS = {"unclear", "unknown", "unable to detect", CANT_TELL}


_now_iso = partial(utc_now_iso, timespec="seconds")


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


# A coding task's answers scored against OTHER runs of the same videos (see
# :func:`score_against_run`): one file per task, keyed by the other run's id.
_CROSS_RESULTS_FILENAME = "results_coding_other_runs.json"


def _check_task_type(task_type: str) -> None:
    """Raise ``ValueError`` on an unknown task type."""
    if task_type not in TASK_TYPES:
        raise ValueError(f"unknown task type: {task_type!r}")


def _contract_enum_columns(contract: dict) -> dict[str, str]:
    """Return ``{output_column: enum_name}`` for every enum-backed contract field.

    Covers scalar fields (``enum = "name"``) and object sub-keys whose ``spec``
    references an enum (``spec = "enum:name"``), keyed on the final post-recode
    column name.
    """
    out: dict[str, str] = {}
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
                    out[ac.contract_output_column(name, key)] = match.group(1)
        elif field.get("enum") and field["enum"] in enums:
            out[ac.contract_output_column(name)] = field["enum"]
    return out


def _contract_enum_values(contract: dict) -> dict[str, list[str]]:
    """Return ``{output_column: [values]}`` for every enum-backed contract field."""
    return {
        col: ac.enum_values(contract, enum)
        for col, enum in _contract_enum_columns(contract).items()
    }


def _contract_enum_descriptions(contract: dict) -> dict[str, dict[str, str]]:
    """Return ``{output_column: {value: definition}}`` for described enums.

    The per-value definitions the model is shown (e.g. each content category),
    so coders can read the same definitions.
    """
    out = {}
    for col, enum in _contract_enum_columns(contract).items():
        described = ac.enum_descriptions(contract, enum)
        if described:
            out[col] = dict(described)
    return out


def available_variables(run_id: str) -> list[dict]:
    """Return the variables of a finished run a human task can cover.

    The union of every compared column in the run report, each with the kind
    the machine comparison used, a display label, the live contract's value
    list for enum/list columns that declare one (a category without one is
    open: coders type the answer), and what the coder form offers beyond the
    codebook (see :func:`_coding_options`). A single-contract run has no
    comparisons, so its columns are read off the arm's own output instead
    (see :func:`_arm_catalog`).

    Args:
        run_id: the finished run's id.

    Returns:
        ``[{name, kind, label, description, values, value_descriptions, open,
        cant_tell, option_labels, dash_label, range}]`` sorted by label.
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
    enum_descriptions = _contract_enum_descriptions(contract) if contract else {}
    col_meta = ac.contract_column_metadata(contract) if contract else {}
    ranges = ac.contract_numeric_ranges(contract) if contract else {}
    vs = _cf().get("var_schema")
    vs_meta: dict[str, dict] = {}
    if vs is not None:
        for _, row in vs.iterrows():
            vs_meta[str(row.get("variable_name"))] = {
                "label": row.get("display_name"),
                "description": row.get("description"),
            }

    if not kinds:
        kinds = _arm_catalog(run_id, run.get("manifest") or {}, contract)

    out = []
    for col, kind in kinds.items():
        meta = vs_meta.get(col) or {}
        cmeta = col_meta.get(col) or {}
        label = meta.get("label") or cmeta.get("display_name") or col
        description = meta.get("description") or cmeta.get("description") or ""
        values = enum_map.get(col)
        out.append(
            {
                "name": col,
                "kind": kind,
                "label": str(label) if pd.notna(label) else col,
                "description": str(description) if pd.notna(description) else "",
                "values": values,
                "value_descriptions": enum_descriptions.get(col) or {},
                **_coding_options(col, kind, values, ranges.get(col)),
            }
        )
    return sorted(out, key=lambda v: v["label"].lower())


# Field-spec keys describing what the coder form offers (snapshotted per task).
_OPTION_KEYS = (
    "open",
    "cant_tell",
    "option_labels",
    "dash_label",
    "range",
    "value_descriptions",
)


def _coding_options(name: str, kind: str, values: list | None, value_range) -> dict:
    """What the coder form offers for one variable beyond its codebook answers.

    * ``open`` — a category with no codebook list: a typed answer. No
      suggestions are drawn from the contract's answers on the run, since that
      would show coders the model's vocabulary and break the blind coding.
    * ``cant_tell`` — offer an explicit "Can't tell" (categories whose codebook
      has no such answer, and numbers). Lists have none: empty means none.
    * ``option_labels`` — readable wording for a codebook's "-" answer.
    * ``dash_label`` — an open category whose codebook defines "-".
    * ``range`` — a number's codebook scale, which coders answer on.
    """
    is_open = kind == "enum" and values is None
    has_own = any(_fold(v) in _CANT_TELL_WORDS for v in values or [])
    return {
        "open": is_open,
        "cant_tell": kind == "numeric" or (kind == "enum" and not has_own),
        "option_labels": (
            {"-": _DASH_LABELS.get(name, "Doesn't apply")} if values and "-" in values else {}
        ),
        "dash_label": _DASH_LABELS.get(name) if is_open else None,
        "range": list(value_range) if kind == "numeric" and value_range else None,
    }


# Arm-frame bookkeeping columns, never a variable (mirrors compare_arms' skip).
_ARM_META_COLUMNS = {"item_id", "annotated_ok", "annotated_fail"}


def _arm_catalog(run_id: str, manifest: dict, contract: dict | None) -> dict[str, str]:
    """Classify a run's columns from its arm frames, for a run with no comparisons.

    A single-contract run has no arm pair to compare, so its report lists no
    columns. Each arm column is classified exactly as ``compare_arms`` would
    (declared scale first: the live var_schema plus the live contract).

    Returns:
        ``{column: kind}``.
    """
    scales = ab._scale_map() if _cf().get("var_schema") is not None else {}
    if contract:
        scales.update(ab.contract_scale_map(contract))
    frames: dict[str, pd.DataFrame] = {}
    for arm in manifest.get("arms", []):
        name = arm.get("name")
        frame = _arm_frame(run_id, name) if name else None
        if frame is not None:
            frames[name] = frame
    kinds: dict[str, str] = {}
    for frame in frames.values():
        for col in frame.columns:
            if col not in _ARM_META_COLUMNS and col not in kinds:
                kinds[col] = ab._classify(col, frame[col], frame[col], scales)
    return kinds


def create_task(
    run_id: str,
    task_type: str,
    variables: list[str],
    coders: list[str],
    created_by: str = "",
    arms: list[str] | None = None,
    n_videos: int | None = None,
) -> dict:
    """Create a human task on a finished run.

    Snapshots the run's items (with platforms, for the video player) and each
    selected variable's spec, so the task stays self-contained even if the
    live contract or the run's eval set changes later.

    Args:
        run_id: the run to attach the task to (must be ``complete``).
        task_type: ``"coding"`` (blind coding for ICR) or ``"vote"`` (per-item
            blind preference votes between the run's arms).
        coders: usernames to invite (admins are always implicitly invited).
        variables: the compared columns coders provide input for. For a
            ``vote`` task these are the DISPLAY fields shown side-by-side;
            an empty selection defaults to every compared column.
        created_by: audit actor.
        arms: vote tasks only — the contracts humans vote between (a subset
            of the run's arms, at least two). ``None`` means all of them.
        n_videos: coding tasks only — code a random sample of this many of
            the run's videos instead of all of them (e.g. the human
            benchmark for a rating task).

    Rating tasks have their own setup: :func:`fyp.annotation.human_rating.create_task`.

    Returns:
        The stored task definition.

    Raises:
        ValueError: unknown type / run not complete / task exists / bad variables.
    """
    _check_task_type(task_type)
    if task_type == "rating":
        raise ValueError("rating tasks are created as drafts by human_rating.create_task")

    run = ab.load_run(run_id)
    manifest = run.get("manifest") or {}
    if manifest.get("status") != "complete":
        raise ValueError(f"run {run_id} is not complete")
    if load_task(run_id, task_type) is not None:
        raise ValueError(f"run {run_id} already has a {task_type} task — delete it first")

    arm_names = [a.get("name") for a in manifest.get("arms", []) if a.get("name")]
    if task_type == "vote":
        if arms:
            arms = [str(a) for a in arms]
            foreign = [a for a in arms if a not in arm_names]
            if foreign:
                raise ValueError(f"unknown contracts: {', '.join(foreign)}")
            # Keep the manifest's canonical order regardless of pick order.
            arm_names = [a for a in arm_names if a in arms]
        if len(arm_names) < 2:
            raise ValueError("a vote task needs at least two contracts to vote between")

    catalog = {v["name"]: v for v in available_variables(run_id)}
    variables = [str(v) for v in variables]
    if not variables and task_type == "vote":
        variables = list(catalog)
    if not variables:
        raise ValueError("select at least one variable")
    unknown = [v for v in variables if v not in catalog]
    if unknown:
        raise ValueError(f"unknown variables: {', '.join(unknown)}")

    item_ids = [str(i) for i in manifest.get("item_ids", [])]
    if not item_ids:
        raise ValueError(f"run {run_id} has no items")
    if task_type == "coding" and n_videos and 0 < int(n_videos) < len(item_ids):
        keep = set(random.Random(secrets.randbits(64)).sample(item_ids, int(n_videos)))
        item_ids = [i for i in item_ids if i in keep]
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
                **{k: catalog[v].get(k) for k in _OPTION_KEYS},
            }
            for v in variables
        },
        "coders": {
            str(u): {"invited_at": now, "invited_by": created_by, "notified": False}
            for u in dict.fromkeys(str(u) for u in coders)
        },
    }
    if task_type == "vote":
        # Canonical arm order + the seed the per-(item, coder) option
        # permutation derives from. The task file is admin-only, so carrying
        # the arm names here does not break coder-side blindness.
        task["arms"] = arm_names
        # The annotation version behind each contract voted on.
        task["arm_versions"] = {a: ab.arm_versions(manifest).get(a) for a in arm_names}
        task["order_seed"] = secrets.token_hex(16)
    with _INDEX_LOCK:
        data_io.save_json(
            data=task,
            storage_location=ab.LOCATION,
            filename=_human_file(run_id, _task_filename(task_type)),
        )
        _update_tasks_index(_index_entry(task))
    return task


def _kind_to_scale(kind: str) -> str:
    """Map a report comparison kind back onto a declared-scale string."""
    return {"numeric": "numeric", "enum": "categorical", "list": "list", "freetext": "text"}.get(
        kind, "text"
    )


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
    if task_type == "coding":
        filenames.append(_CROSS_RESULTS_FILENAME)
    filenames += [
        _coder_filename(task_type, username) for username in (task or {}).get("coders", {})
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
            t
            for t in list_tasks()
            if not (t.get("run_id") == run_id and t.get("task_type") == task_type)
        ]
        data_io.save_json(data=index, storage_location=ab.LOCATION, filename=TASKS_INDEX_FILENAME)
    return removed


def list_tasks() -> list[dict]:
    """Return the global task index (newest first). Never raises."""
    ab.ensure_locations()
    try:
        if data_io.exists(storage_location=ab.LOCATION, filename=TASKS_INDEX_FILENAME):
            index = data_io.load_json(storage_location=ab.LOCATION, filename=TASKS_INDEX_FILENAME)
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
        "status": task.get("status", "started"),
    }


def _update_tasks_index(entry: dict, submitted: list[str] | None = None) -> None:
    """Insert/replace one task's index entry (keyed by run_id + task_type).

    Callers must hold ``_INDEX_LOCK``. When ``submitted`` is None the previous
    entry's submitted list is preserved.
    """
    index = list_tasks()
    previous = next(
        (
            t
            for t in index
            if t.get("run_id") == entry["run_id"] and t.get("task_type") == entry["task_type"]
        ),
        None,
    )
    if submitted is not None:
        entry = {**entry, "submitted": sorted(set(submitted))}
    elif previous is not None:
        entry = {**entry, "submitted": previous.get("submitted", [])}
    index = [
        t
        for t in index
        if not (t.get("run_id") == entry["run_id"] and t.get("task_type") == entry["task_type"])
    ]
    index.append(entry)
    data_io.save_json(data=index, storage_location=ab.LOCATION, filename=TASKS_INDEX_FILENAME)


def tasks_for_user(username: str, is_admin: bool = False) -> list[dict]:
    """Return the index entries a user may work on (all of them for admins).

    Draft rating tasks are nobody's work yet, so they are never listed.
    """
    tasks = [t for t in list_tasks() if t.get("status") != "draft"]
    if is_admin:
        return tasks
    return [t for t in tasks if str(username) in (t.get("coders") or [])]


def is_invited(task: dict, username: str, is_admin: bool = False) -> bool:
    """True when a user may access a task (admins always may; nobody a draft)."""
    if task.get("status") == "draft":
        return False
    return bool(is_admin) or str(username) in (task.get("coders") or {})


def add_coders(run_id: str, task_type: str, usernames: list[str], invited_by: str = "") -> dict:
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
        if task_type == "rating" and task.get("status") == "started":
            raise ValueError("raters are fixed once a rating task has started")
        now = _now_iso()
        for username in usernames:
            username = str(username)
            if username not in task["coders"]:
                task["coders"][username] = {
                    "invited_at": now,
                    "invited_by": invited_by,
                    "notified": False,
                }
        data_io.save_json(
            data=task,
            storage_location=ab.LOCATION,
            filename=_human_file(run_id, _task_filename(task_type)),
        )
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


# ---------------------------------------------------------------------------
# Vote tasks: blind option permutation + payload.
# ---------------------------------------------------------------------------


def vote_option_letters(n: int) -> list[str]:
    """Return the option labels for ``n`` arms (``["A", "B", ...]``)."""
    return [chr(ord("A") + i) for i in range(n)]


def _vote_permutation(task: dict, item_id: str, username: str) -> list[str]:
    """Return the arm order shown to one coder for one item.

    Deterministic across requests and processes (a pure hash of the task's
    ``order_seed`` + item + coder + arm), yet effectively random across items
    and coders — so a voter cannot learn which option is which arm from
    position, and the same coder always sees a stable order for an item.
    """
    seed = task.get("order_seed", "")
    return sorted(
        task.get("arms", []),
        key=lambda arm: hashlib.sha256(f"{seed}|{item_id}|{username}|{arm}".encode()).hexdigest(),
    )


def resolve_vote_choice(task: dict, item_id: str, username: str, choice: str) -> str:
    """Resolve a coder's posted option letter (or ``"tie"``) to an arm name.

    The coder client only ever knows letters; the mapping back to arm names
    happens here, server-side, so the stored response carries the arm.

    Raises:
        ValueError: on anything that is not ``"tie"`` or a valid letter.
    """
    choice = str(choice).strip()
    if choice.lower() == "tie":
        return "tie"
    order = _vote_permutation(task, str(item_id), str(username))
    letters = vote_option_letters(len(order))
    if choice.upper() in letters:
        return order[letters.index(choice.upper())]
    raise ValueError(f"invalid vote choice: {choice!r}")


def letter_for_arm(task: dict, item_id: str, username: str, arm: str) -> str:
    """Inverse of :func:`resolve_vote_choice` for re-serving saved responses."""
    if arm == "tie":
        return "tie"
    order = _vote_permutation(task, str(item_id), str(username))
    letters = vote_option_letters(len(order))
    try:
        return letters[order.index(arm)]
    except ValueError:
        return ""


def vote_options_payload(task: dict, username: str) -> dict[str, list[dict]]:
    """Build the blind per-item option list for one coder.

    Each option carries only its letter and the display-variable values of one
    arm's refined row — never the arm name, etag or source — in the coder's
    per-item permuted order. A missing arm row yields empty values (the arm
    failed that item), which is itself information the voter should see.

    Args:
        task: the vote task definition.
        username: the coder (drives the permutation).

    Returns:
        ``{item_id: [{"option": "A", "values": {var: value}}, ...]}``.
    """
    variables = task.get("variables", [])
    rows_by_arm: dict[str, dict[str, dict]] = {}
    for arm in task.get("arms", []):
        try:
            rows = ab.load_run_rows(task["run_id"], arm)
        except Exception:
            rows = []
        rows_by_arm[arm] = {str(r.get("item_id")): r for r in rows}

    out: dict[str, list[dict]] = {}
    for item_id in (str(i) for i in task.get("item_ids", [])):
        order = _vote_permutation(task, item_id, str(username))
        letters = vote_option_letters(len(order))
        options = []
        for letter, arm in zip(letters, order, strict=True):
            row = rows_by_arm.get(arm, {}).get(item_id, {})
            options.append(
                {
                    "option": letter,
                    "values": {v: row.get(v, "") for v in variables},
                }
            )
        out[item_id] = options
    return out


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
            elif value == CANT_TELL and spec.get("cant_tell"):
                cleaned[var] = CANT_TELL
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
            accepted = None if spec.get("open") else spec.get("values")
            cant_tell = value == CANT_TELL and spec.get("cant_tell")
            if value and accepted and value not in accepted and not cant_tell:
                raise ValueError(f"{var}: {value!r} is not an accepted value")
            cleaned[var] = value
        else:  # freetext
            cleaned[var] = str(value)
    return cleaned


def save_response(
    run_id: str,
    task_type: str,
    username: str,
    item_id: str,
    values: dict | None,
    note: str | None = None,
) -> dict:
    """Save one coder's response for one item (autosave granularity).

    Writes only that coder's own file, so concurrent coders never collide.

    Args:
        run_id: the task's run.
        task_type: the task's type.
        username: the coder.
        item_id: the coded item (must belong to the task).
        values: ``{variable: value}`` (validated against the task specs), or
            ``None`` to keep the item's existing values (a note-only save).
        note: optional free-text note for the item; ``None`` keeps the
            existing note, an empty string clears it.

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

    existing = state["responses"].get(str(item_id)) or {}
    if values is None:
        # Note-only save: keep whatever values are already recorded (lets a
        # voter jot a note before — or without — picking an option).
        cleaned = existing.get("values") or {}
    elif task_type == "vote":
        # A vote is exactly one key: the option letter (or "tie"), resolved to
        # the underlying arm name here so the client never learns the mapping.
        if set(values or {}) != {"choice"}:
            raise ValueError("a vote must be exactly {'choice': <option>}")
        cleaned = {"choice": resolve_vote_choice(task, item_id, username, values["choice"])}
    elif task_type == "rating":
        from fyp.annotation import human_rating

        cleaned = human_rating.validate_rating(task, username, item_id, values)
    else:
        cleaned = _validate_values(task, values)

    now = _now_iso()
    state["started_at"] = state.get("started_at") or now
    state["updated_at"] = now
    response = {
        "values": cleaned,
        "note": (
            str(note).strip()[:MAX_NOTE_CHARS]
            if note is not None
            else str(existing.get("note") or "")
        ),
        "updated_at": now,
    }
    if not response["note"]:
        response.pop("note")
    state["responses"][str(item_id)] = response
    data_io.save_json(
        data=state,
        storage_location=ab.LOCATION,
        filename=_human_file(run_id, _coder_filename(task_type, username)),
    )
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
    if not _n_answered(state):
        raise ValueError("nothing to submit — no items answered")

    now = _now_iso()
    state["status"] = "submitted"
    state["submitted_at"] = now
    state["updated_at"] = now
    data_io.save_json(
        data=state,
        storage_location=ab.LOCATION,
        filename=_human_file(run_id, _coder_filename(task_type, username)),
    )

    with _INDEX_LOCK:
        index = list_tasks()
        previous = next(
            (t for t in index if t.get("run_id") == run_id and t.get("task_type") == task_type),
            None,
        )
        submitted = set((previous or {}).get("submitted", [])) | {str(username)}
        _update_tasks_index(_index_entry(task), submitted=sorted(submitted))

    try:
        compute_results(run_id, task_type)
    except Exception as exc:
        logger.error(f"[human_eval] results computation failed for {run_id}/{task_type}: {exc}")

    return {"n_answered": _n_answered(state), "n_items": len(task.get("item_ids", []))}


def _n_answered(state: dict) -> int:
    """Count the items a coder actually answered (a bare note is not an answer)."""
    return sum(1 for response in (state.get("responses") or {}).values() if response.get("values"))


def _database_frame(task: dict, state: dict) -> pd.DataFrame:
    """One coder's answers in the database's own categories, ready to compare.

    The contract's answers reached the run's arm frames through the production
    recode, so a coder's answers take the same route before any metric sees
    them (``ab_eval.refine_from_flat_dicts``, minus its stray-key column
    merging). "Fashion & Beauty" becomes "fashion and beauty", a 0-100 score is
    rescaled to 0-1, an empty list reads "unable to detect", and "doesn't
    apply" or "can't tell" land in the merged label the database uses for the
    field ("other category" for a closed list, "unable to detect" for an open
    category). A skipped answer, and "can't tell" on a number, stay missing.

    One row per coded item. ``annotated_ok`` is set so ``compare_arms`` leaves
    out items the contract failed to annotate.
    """
    specs = task.get("field_specs", {})
    variables = task.get("variables", [])
    splitter = _cf()["labels"]["SPLITTER"]
    records: list[dict] = []
    skipped: list[tuple[str, str]] = []
    for item_id, response in (state.get("responses") or {}).items():
        values = response.get("values") or {}
        if not values:
            continue  # note-only response — not a coded item
        row: dict = {"item_id": str(item_id)}
        for var in variables:
            value = values.get(var)
            kind = (specs.get(var) or {}).get("kind")
            if value is None or value == "" or (kind == "numeric" and value == CANT_TELL):
                skipped.append((str(item_id), var))
                value = None
            elif value == CANT_TELL:
                value = "-"  # the recode merges it exactly like the codebook's "-"
            elif isinstance(value, list):
                value = splitter.join(str(v) for v in value)
            row[var] = value
        records.append(row)
    if not records:
        return pd.DataFrame(columns=["item_id", *variables, "annotated_ok"])

    flat = pd.DataFrame(records).set_index("item_id")
    frame = ab.refine_from_flat_dicts(records, consolidate_columns=False)
    frame = frame.astype(object).set_index(frame["item_id"].astype(str))
    for var in variables:
        if var not in frame.columns:  # a column the live var_schema does not know
            frame[var] = flat[var]
    frame = frame[variables].copy()
    for item_id, var in skipped:
        frame.at[item_id, var] = None
    frame["annotated_ok"] = True
    return frame.rename_axis("item_id").reset_index()


def _arm_frame(run_id: str, arm: str, variables: list[str] | None = None) -> pd.DataFrame | None:
    """Load one machine arm's refined frame, restricted to ``variables`` when given."""
    try:
        frame = data_io.load_parquet(
            storage_location=ab.LOCATION,
            filename=ab._run_file(run_id, f"arm_{arm}.parquet"),
        )
    except Exception:
        return None
    if frame is None or "item_id" not in frame.columns:
        return None
    if variables is not None:
        frame = frame[["item_id"] + [v for v in variables if v in frame.columns]]
    frame = frame.copy()
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


# ---------------------------------------------------------------------------
# Krippendorff's alpha.
# ---------------------------------------------------------------------------

# Level of measurement per comparison kind. Free text has none: two prose
# answers are never equal, so a reliability coefficient says nothing there.
_ALPHA_LEVELS = {"enum": "nominal", "numeric": "interval", "list": "set"}

# Inside a list these mean "nothing present", so ["none"] is the empty set.
# Every other answer — "other", "unknown", "unclear" included — counts as given.
_LIST_NOTHING = {"", "none", "n/a", "-", "--", "<na>"}


def _jaccard_distance(a: frozenset, b: frozenset) -> float:
    """1 − |A∩B| / |A∪B|, with two empty sets at distance 0."""
    union = a | b
    return 1.0 - len(a & b) / len(union) if union else 0.0


_ALPHA_DISTANCES = {
    "nominal": lambda a, b: 0.0 if a == b else 1.0,
    "interval": lambda a, b: (a - b) ** 2,
    "set": _jaccard_distance,
}


def krippendorff_alpha(units: list[list], level: str) -> dict:
    """Krippendorff's alpha over ``units`` — one list of observed values per video.

    Missing answers are already left out of each unit; a unit with fewer than
    two values cannot be paired and drops out, as the coefficient prescribes.
    ``alpha = 1 − D_o / D_e``: the mean distance between answers to the same
    video over the mean distance between any two answers.

    Args:
        units: per-video value lists (strings for ``nominal``, floats for
            ``interval``, frozensets for ``set``, ranks such as 0, 1, 2 for
            ``ordinal``).
        level: ``nominal`` / ``interval`` / ``set`` (Jaccard distance) /
            ``ordinal`` (Krippendorff's rank metric, which depends on how
            often each value occurs).

    Returns:
        ``{alpha, n_units, n_values}`` over the pairable units; ``alpha`` is
        ``None`` when fewer than two values pair up or every answer is the same
        (the coefficient is undefined without variation).
    """
    pairable = [unit for unit in units if len(unit) >= 2]
    if level == "ordinal":
        totals = Counter(v for unit in pairable for v in unit)
        ranks = sorted(totals)

        def delta(a, b) -> float:
            low, high = sorted((a, b))
            between = sum(totals[g] for g in ranks if low <= g <= high)
            return (between - (totals[low] + totals[high]) / 2) ** 2

    else:
        delta = _ALPHA_DISTANCES[level]

    def _pair_sum(counts: Counter) -> float:
        # Σ over ordered pairs of distinct observations; δ(c, c) = 0 always.
        values = list(counts)
        return sum(counts[a] * counts[b] * delta(a, b) for a in values for b in values if a != b)

    n = sum(len(unit) for unit in pairable)
    out = {"alpha": None, "n_units": len(pairable), "n_values": n}
    if n < 2:
        return out
    observed = sum(_pair_sum(Counter(unit)) / (len(unit) - 1) for unit in pairable) / n
    expected = _pair_sum(Counter(v for unit in pairable for v in unit)) / (n * (n - 1))
    if expected == 0:
        return out
    out["alpha"] = float(1.0 - observed / expected)
    return out


def _fold(value) -> str:
    """Case-, space- and dash-insensitive text of one answer."""
    return str(value).strip().translate(ab._DASH_FOLD).lower()


def _alpha_value(value, kind: str):
    """One answer as an alpha observation, or ``None`` when there is none.

    Answers count as given: unlike the agreement metrics, "none", "unknown"
    and "other" are categories of their own. Only case, surrounding spaces and
    dash style are ignored. A list cell arrives as a list/array or as a
    ``SPLITTER``-joined string (see ``ab_eval._to_set``); an empty list is the
    answer "nothing", not a missing one.
    """
    if not isinstance(value, (list, tuple, np.ndarray)):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    if kind == "numeric":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return None if np.isnan(number) else number
    if kind == "list":
        if isinstance(value, (list, tuple, np.ndarray)):
            parts = [ab._normalize_cell(v) for v in value]
        else:
            norm = ab._normalize_cell(value)
            splitter = _cf()["labels"]["SPLITTER"]
            parts = norm.split(splitter) if (splitter and splitter in norm) else [norm]
        return frozenset(f for f in (_fold(p) for p in parts) if f not in _LIST_NOTHING)
    text = _fold(value)
    return text or None


def _alpha_column(frame: pd.DataFrame, variable: str, kind: str) -> dict:
    """``{item_id: observation}`` for one source's answers to one variable.

    A video the contract failed to annotate (``annotated_ok`` false) has no
    answers rather than empty ones, as in ``compare_arms``.
    """
    if variable not in frame.columns:
        return {}
    if "annotated_ok" in frame.columns:
        frame = frame[frame["annotated_ok"].fillna(False).astype(bool)]
    out: dict = {}
    for item_id, value in zip(frame["item_id"].astype(str), frame[variable], strict=False):
        observation = _alpha_value(value, kind)
        if observation is not None and item_id not in out:
            out[item_id] = observation
    return out


def _no_answer_labels() -> set[str]:
    """The database's merged "no answer" labels for a single-category field.

    What "doesn't apply", "can't tell" and a blank answer become after the
    recode ("other category" / "unable to detect" / "not coded"), plus the
    codebook's own can't-tell answers ("unclear", "unknown").
    """
    labels = _cf()["labels"]
    merged = (labels["OTHER_THINGS"], labels["UNABLE_TO_DETECT"], labels["NOT_CODED"])
    return {_fold(v) for v in merged} | {"unclear", "unknown"}


def _alpha_results(
    task: dict, coder_frames: dict[str, pd.DataFrame], arm_frames: dict[str, pd.DataFrame]
) -> dict:
    """Krippendorff's alpha per variable: the coders alone, and with each contract.

    ``humans`` needs two submitted coders; ``with_arm[arm]`` pools every
    submitted coder with that one contract as a further coder. Both compare
    answers in the database's categories (see :func:`_database_frame`), where
    the merged "no answer" label counts as an answer. For single-category
    fields the ``*_check`` figures treat that label as missing on both sides
    instead, and ``no_answer_share`` says how often each side gave it.
    """
    item_ids = [str(i) for i in task.get("item_ids", [])]
    no_answer = _no_answer_labels()

    def _units(columns: list[dict]) -> list[list]:
        return [[col[i] for col in columns if i in col] for i in item_ids]

    def _answered(column: dict) -> dict:
        return {i: v for i, v in column.items() if v not in no_answer}

    def _share(columns: list[dict]) -> float | None:
        values = [v for col in columns for v in col.values()]
        return sum(v in no_answer for v in values) / len(values) if values else None

    def _alphas(humans: list[dict], arms: dict[str, dict], level: str) -> tuple:
        pooled = krippendorff_alpha(_units(humans), level) if len(humans) >= 2 else None
        with_arm = (
            {arm: krippendorff_alpha(_units([*humans, col]), level) for arm, col in arms.items()}
            if humans
            else {}
        )
        return pooled, with_arm

    out: dict = {}
    for variable in task.get("variables", []):
        kind = (task.get("field_specs", {}).get(variable) or {}).get("kind")
        level = _ALPHA_LEVELS.get(kind)
        if level is None:
            continue
        by_coder = {u: _alpha_column(f, variable, kind) for u, f in coder_frames.items()}
        humans = list(by_coder.values())
        arms = {arm: _alpha_column(f, variable, kind) for arm, f in arm_frames.items()}
        pooled, with_arm = _alphas(humans, arms, level)
        entry = {
            "kind": kind,
            "level": level,
            "n_coders": len(humans),
            "humans": pooled,
            "with_arm": with_arm,
        }
        if kind == "enum":
            pooled_check, with_arm_check = _alphas(
                [_answered(c) for c in humans], {a: _answered(c) for a, c in arms.items()}, level
            )
            entry["humans_check"] = pooled_check
            entry["with_arm_check"] = with_arm_check
            entry["no_answer_share"] = {
                "coders": _share(humans),
                "by_coder": {u: _share([c]) for u, c in by_coder.items()},
                "arms": {a: _share([c]) for a, c in arms.items()},
            }
        out[variable] = entry
    return out


def compute_results(run_id: str, task_type: str) -> dict:
    """(Re)compute a task's ICR metrics and persist them.

    Compares every *submitted* coder against every machine arm, and every
    submitted-coder pair against each other, using ``ab_eval.compare_arms``
    with the task's snapshotted scales — then injects Cohen's kappa per enum
    column, and adds Krippendorff's alpha per variable over every answer as
    given (see :func:`_alpha_results`).

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
    if task_type == "vote":
        return _compute_vote_results(run_id, task)
    if task_type == "rating":
        from fyp.annotation import human_rating

        return human_rating.compute_results(run_id, task)
    variables = task.get("variables", [])
    scales = {v: spec.get("scale", "text") for v, spec in task.get("field_specs", {}).items()}

    coder_frames = _submitted_coder_frames(run_id, task)

    manifest = ab.load_run(run_id).get("manifest") or {}
    arm_names = [a.get("name") for a in manifest.get("arms", []) if a.get("name")]
    arm_frames: dict[str, pd.DataFrame] = {}
    for arm in arm_names:
        # annotated_ok rides along for alpha; compare_arms ignores it here,
        # since coder frames do not carry it.
        frame = _arm_frame(run_id, arm, [*variables, "annotated_ok"])
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
            comp = ab.compare_arms(coder_frames[coders[i]], coder_frames[coders[j]], scales=scales)
            human_vs_human[key] = _inject_kappa(
                comp, coder_frames[coders[i]], coder_frames[coders[j]]
            )

    per_coder: dict[str, dict] = {}
    for username in coders:
        own = [
            comp["summary"]
            for key, comp in human_vs_machine.items()
            if key.split("|", 1)[0] == username
        ]
        per_coder[username] = {
            metric: _mean_of(own, metric)
            for metric in (
                "mean_enum_agreement",
                "mean_enum_agreement_filled",
                "mean_enum_kappa",
                "mean_list_jaccard",
                "mean_numeric_correlation",
            )
        }
        per_coder[username]["n_items_coded"] = int(len(coder_frames[username]))

    results = {
        "computed_at": _now_iso(),
        "variables": variables,
        "coders": coders,
        "arms": sorted(arm_frames),
        "arm_versions": {a: v for a, v in ab.arm_versions(manifest).items() if a in arm_frames},
        "human_vs_machine": human_vs_machine,
        "human_vs_human": human_vs_human,
        "alpha": _alpha_results(task, coder_frames, arm_frames),
        "summary": {"per_coder": per_coder},
    }
    data_io.save_json(
        data=results,
        storage_location=ab.LOCATION,
        filename=_human_file(run_id, _results_filename(task_type)),
    )
    return results


def _submitted_coder_frames(run_id: str, task: dict) -> dict[str, pd.DataFrame]:
    """Each submitted coder's answers in the database's categories."""
    frames: dict[str, pd.DataFrame] = {}
    for username in task.get("coders", {}):
        state = load_coder_state(run_id, task["task_type"], username)
        if state.get("status") == "submitted" and state.get("responses"):
            frames[username] = _database_frame(task, state)
    return frames


def score_against_run(task_run_id: str, other_run_id: str) -> dict:
    """Score a coding task's stored answers against another run of the same videos.

    Human coding is done once, on one run. When the contract, the model or a
    setting changes, a new test run annotates the same videos under the new
    annotation version, and this function says how well the new version agrees
    with the people who already coded them: Krippendorff's alpha per variable
    for the coders alone, and with each contract of the other run added as one
    more coder. It is the calculation the task's own results use
    (:func:`_alpha_results`): coder answers pass through the production recode
    (:func:`_database_frame`) and are compared with the other run's answers in
    the database's categories, on the task's snapshotted variable kinds.

    Nothing is collected and nothing reaches the coders: the function reads
    the submitted codings and the other run's stored answers, and writes its
    result next to the task's results, which only admin endpoints serve.

    Args:
        task_run_id: the run whose coding task holds the human answers.
        other_run_id: a finished run whose videos include every coded video.

    Returns:
        ``{computed_at, task_run_id, run_id, run_name, coders, variables,
        n_items, arms, arm_versions, task_arm_versions, alpha,
        missing_variables}``. ``alpha`` has the shape of the task results'
        ``alpha`` block; a contract that does not produce a variable has
        ``None`` there and is listed under ``missing_variables``.

    Raises:
        ValueError: no coding task, no submitted codings, the other run is
            missing or unfinished, or it lacks some of the coded videos.
    """
    task = load_task(task_run_id, "coding")
    if task is None:
        raise ValueError(f"run {task_run_id} has no coding task")
    if str(other_run_id) == str(task_run_id):
        raise ValueError("pick a different run: the task's own run is scored in its results")
    other_manifest = ab.load_run(other_run_id).get("manifest") or {}
    if not other_manifest:
        raise ValueError(f"run {other_run_id} not found")
    if other_manifest.get("status") != "complete":
        raise ValueError(f"run {other_run_id} is not complete")
    coded = [str(i) for i in task.get("item_ids", [])]
    available = {str(i) for i in other_manifest.get("item_ids", [])}
    absent = [i for i in coded if i not in available]
    if absent:
        raise ValueError(
            f"run {other_run_id} lacks {len(absent)} of the {len(coded)} coded videos "
            "— it must have annotated every video the coders answered"
        )

    coder_frames = _submitted_coder_frames(task_run_id, task)
    if not coder_frames:
        raise ValueError(f"run {task_run_id}: no coder has submitted yet")

    variables = task.get("variables", [])
    arm_names = [a.get("name") for a in other_manifest.get("arms", []) if a.get("name")]
    arm_frames: dict[str, pd.DataFrame] = {}
    missing: dict[str, list[str]] = {}
    for arm in arm_names:
        frame = _arm_frame(other_run_id, arm, [*variables, "annotated_ok"])
        if frame is None:
            continue
        arm_frames[arm] = frame
        lacking = [v for v in variables if v not in frame.columns]
        if lacking:
            missing[arm] = lacking

    alpha = _alpha_results(task, coder_frames, arm_frames)
    # A contract that never answers a variable is not a coder of it: without
    # this its "with the contract" figure would silently repeat the coders'.
    for arm, lacking in missing.items():
        for variable in lacking:
            entry = alpha.get(variable)
            if entry is None:
                continue
            entry["with_arm"][arm] = None
            if "with_arm_check" in entry:
                entry["with_arm_check"][arm] = None
                entry["no_answer_share"]["arms"][arm] = None

    task_manifest = ab.load_run(task_run_id).get("manifest") or {}
    result = {
        "computed_at": _now_iso(),
        "task_run_id": task_run_id,
        "run_id": other_run_id,
        "run_name": other_manifest.get("name") or "",
        "coders": sorted(coder_frames),
        "variables": variables,
        "n_items": len(coded),
        "arms": sorted(arm_frames),
        # Which annotation version each contract ran under, on both runs, so
        # the figures are tied to versions and not only to runs.
        "arm_versions": {a: ab.arm_versions(other_manifest).get(a) for a in sorted(arm_frames)},
        "task_arm_versions": ab.arm_versions(task_manifest),
        "alpha": alpha,
        "missing_variables": missing,
    }
    with _INDEX_LOCK:
        stored = load_cross_run_results(task_run_id)
        stored[str(other_run_id)] = result
        data_io.save_json(
            data=stored,
            storage_location=ab.LOCATION,
            filename=_human_file(task_run_id, _CROSS_RESULTS_FILENAME),
        )
    return result


def load_cross_run_results(task_run_id: str) -> dict:
    """Stored :func:`score_against_run` results of a coding task, by other run id."""
    ab.ensure_locations()
    filename = _human_file(task_run_id, _CROSS_RESULTS_FILENAME)
    try:
        if data_io.exists(storage_location=ab.LOCATION, filename=filename):
            stored = data_io.load_json(storage_location=ab.LOCATION, filename=filename)
            if isinstance(stored, dict):
                return stored
    except Exception:
        pass
    return {}


def _compute_vote_results(run_id: str, task: dict) -> dict:
    """(Re)compute a vote task's win/tie tallies and persist them.

    Win rates are computed over non-tie votes. For a two-arm task a simple
    sign test (binomial, null p=0.5 over the non-tie votes) is included; with
    more arms it is ``None`` (a multinomial test is not obviously the right
    reading there).
    """
    arms = task.get("arms", [])
    per_coder: dict[str, dict] = {}
    pooled_wins = dict.fromkeys(arms, 0)
    pooled_ties = 0
    for username in task.get("coders", {}):
        state = load_coder_state(run_id, task["task_type"], username)
        if state.get("status") != "submitted" or not state.get("responses"):
            continue
        wins = dict.fromkeys(arms, 0)
        ties = 0
        for response in state["responses"].values():
            choice = (response.get("values") or {}).get("choice")
            if choice == "tie":
                ties += 1
            elif choice in wins:
                wins[choice] += 1
        per_coder[username] = {
            "wins": wins,
            "ties": ties,
            "n_votes": sum(wins.values()) + ties,
        }
        for arm, n in wins.items():
            pooled_wins[arm] += n
        pooled_ties += ties

    n_non_tie = sum(pooled_wins.values())
    n_votes = n_non_tie + pooled_ties
    win_rates = {arm: (n / n_non_tie if n_non_tie else None) for arm, n in pooled_wins.items()}

    sign_test = None
    if len(arms) == 2 and n_non_tie > 0:
        from scipy.stats import binomtest

        k_first = pooled_wins[arms[0]]
        sign_test = {
            "arms": arms,
            "n_non_tie": n_non_tie,
            "k_wins_first": k_first,
            "p_value": float(binomtest(k_first, n_non_tie, 0.5).pvalue),
        }

    results = {
        "computed_at": _now_iso(),
        "arms": arms,
        "coders": sorted(per_coder),
        "n_items": len(task.get("item_ids", [])),
        "per_coder": per_coder,
        "pooled": {
            "wins": pooled_wins,
            "ties": pooled_ties,
            "n_votes": n_votes,
            "win_rates": win_rates,
        },
        "tie_rate": (pooled_ties / n_votes) if n_votes else None,
        "sign_test": sign_test,
    }
    data_io.save_json(
        data=results,
        storage_location=ab.LOCATION,
        filename=_human_file(run_id, _results_filename(task["task_type"])),
    )
    return results


def set_notified(run_id: str, task_type: str, username: str) -> None:
    """Record that a coder's invitation email was sent (tolerant no-op).

    Called from the email sender's success callback, possibly from a
    background thread and possibly after the task was deleted or the coder
    uninvited — both cases silently do nothing.
    """
    with _INDEX_LOCK:
        task = load_task(run_id, task_type)
        if task is None or str(username) not in task.get("coders", {}):
            return
        task["coders"][str(username)]["notified"] = True
        task["coders"][str(username)]["notified_at"] = _now_iso()
        data_io.save_json(
            data=task,
            storage_location=ab.LOCATION,
            filename=_human_file(run_id, _task_filename(task_type)),
        )


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


def coder_rows(run_id: str, task_type: str, username: str) -> list[dict]:
    """Return one coder's responses as display rows (the human analogue of
    ``ab_eval.load_run_rows``).

    One row per responded item: ``item_id`` + the coder's values, normalized
    to strings exactly like machine rows (lists render as ``[a, b]``), plus a
    ``note`` key when the coder left one. Feeds the run report's per-item
    modal and the client-side value distributions for human "arms".
    """
    state = load_coder_state(run_id, task_type, username)
    rows = []
    for item_id, response in (state.get("responses") or {}).items():
        row = {"item_id": str(item_id)}
        for var, value in (response.get("values") or {}).items():
            row[var] = ab._normalize_cell(value)
        note = str(response.get("note") or "").strip()
        if note:
            row["note"] = note
        rows.append(row)
    return rows


# Leading characters a spreadsheet reads as a formula.
_FORMULA_LEAD = ("=", "+", "-", "\t", "\r")


def _export_text(value) -> str:
    """One answer as CSV text, as given: lists joined with "; ", blanks empty."""
    if isinstance(value, (list, tuple, np.ndarray)):
        return "; ".join(_export_text(v) for v in value)
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return str(int(number)) if number.is_integer() else repr(number)
    return str(value)


def _export_cell(value) -> str:
    """``_export_text`` made safe to open in a spreadsheet.

    Coders type notes and list entries, so text that would start a formula
    gets a leading apostrophe. Numbers keep their sign and a bare "-" / "--"
    answer (a contract's "nothing") stays as written: neither can run anything.
    """
    text = _export_text(value)
    if text.startswith(_FORMULA_LEAD) and text.strip("-"):
        try:
            float(text)
        except ValueError:
            return "'" + text
    return text


def _readable_answer(value, spec: dict | None):
    """A coder's answer with a codebook "-" spelled out as the option they picked."""
    spec = spec or {}
    label = (spec.get("option_labels") or {}).get("-") or spec.get("dash_label")
    if not label:
        return value
    if isinstance(value, list):
        return [label if v == "-" else v for v in value]
    return label if value == "-" else value


def export_codings_csv(run_id: str, task_type: str = "coding") -> str:
    """Every submitted coding next to each contract's answer, as CSV text.

    One row per video in task order: ``item_id``, ``platform``, then for each
    task variable one column per contract (``{variable}__{arm}``) followed by
    one per submitted coder (``{variable}__human:{username}``), and last each
    coder's note (``note__human:{username}``). Only submitted coders appear —
    the same set the agreement statistics use. Contract columns hold the
    stored (recoded) answers; coder columns hold each answer as the coder gave
    it, with a codebook "-" spelled out as the option they picked (e.g. "No
    person in the video") and "can't tell" as written. A skipped answer is a
    blank cell. Starts with a UTF-8 byte-order mark so spreadsheets read accents.

    Raises:
        ValueError: when the task does not exist or is not a coding task.
    """
    if task_type != "coding":
        raise ValueError("only coding tasks can be exported")
    task = load_task(run_id, task_type)
    if task is None:
        raise ValueError(f"run {run_id} has no {task_type} task")
    variables = task.get("variables", [])
    specs = task.get("field_specs", {})

    coders: dict[str, dict] = {}
    for username in sorted(task.get("coders", {})):
        state = load_coder_state(run_id, task_type, username)
        if state.get("status") == "submitted":
            coders[username] = state.get("responses") or {}

    manifest = ab.load_run(run_id).get("manifest") or {}
    arms: dict[str, dict] = {}
    for arm in (a.get("name") for a in manifest.get("arms", []) if a.get("name")):
        frame = _arm_frame(run_id, arm, variables)
        if frame is not None:
            arms[arm] = {
                str(row["item_id"]): row
                for row in frame.drop_duplicates("item_id").to_dict("records")
            }

    header = ["item_id", "platform"]
    for variable in variables:
        header += [f"{variable}__{arm}" for arm in arms]
        header += [f"{variable}__human:{username}" for username in coders]
    header += [f"note__human:{username}" for username in coders]

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    platforms = {str(i["item_id"]): i.get("platform", "") for i in task.get("items", [])}
    for item_id in (str(i) for i in task.get("item_ids", [])):
        row = [item_id, platforms.get(item_id, "")]
        for variable in variables:
            row += [_export_cell(arms[arm].get(item_id, {}).get(variable)) for arm in arms]
            row += [
                _export_cell(
                    _readable_answer(
                        ((responses.get(item_id) or {}).get("values") or {}).get(variable),
                        specs.get(variable),
                    )
                )
                for responses in coders.values()
            ]
        row += [
            _export_cell(str((responses.get(item_id) or {}).get("note") or "").strip())
            for responses in coders.values()
        ]
        writer.writerow(row)
    return "\ufeff" + buffer.getvalue()


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
            "n_answered": _n_answered(state),
            "updated_at": state.get("updated_at"),
            "submitted_at": state.get("submitted_at"),
        }
    return out


def collect_notes(run_id: str, task_type: str, task: dict | None = None) -> list[dict]:
    """Return every coder note on a task, newest first.

    Includes notes from not-yet-submitted coders — a note is context for the
    admin reading the report, not a metric input.
    """
    task = task if task is not None else load_task(run_id, task_type)
    notes = []
    for username in (task or {}).get("coders", {}):
        state = load_coder_state(run_id, task_type, username)
        for item_id, response in (state.get("responses") or {}).items():
            note = str(response.get("note") or "").strip()
            if note:
                notes.append(
                    {
                        "username": username,
                        "item_id": str(item_id),
                        "note": note,
                        "updated_at": response.get("updated_at"),
                    }
                )
    return sorted(notes, key=lambda n: n.get("updated_at") or "", reverse=True)


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
            "status": task.get("status", "started"),
            "n_items": len(task.get("item_ids", [])),
            "variables": task.get("variables", []),
            "coder_status": coder_status(run_id, task_type, task=task),
            "results": load_results(run_id, task_type),
            "notes": collect_notes(run_id, task_type, task=task),
        }
    return out or None
