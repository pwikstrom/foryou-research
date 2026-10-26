"""Quality-rating tasks: people judge the model's answers against the video.

A third human-task type next to blind coding and preference votes (see
:mod:`fyp.annotation.human_eval`, whose storage, invitations and per-coder
files it shares; raters are kept in the task's ``coders`` map). Free text and
other open answers cannot be checked by agreement, so raters watch each video
and judge ONE answer per variable, without learning where it came from:

* ``model`` — the contract's answer (the run arm being evaluated);
* ``human:<coder>`` — what a coder wrote for that video in the run's coding
  task (a benchmark);
* ``swap:<rater>`` — a trap: the model's answer to the same question for a
  different video (an attention check);
* ``trap:<id>`` — a trap the researcher wrote in the draft: a copy of the
  model's answer edited to plant one false detail.

A task is a DRAFT until :func:`start`: traps are written then. Start freezes
every answer, plans which rater sees which (every rater sees each rated video
once; raters never see their own coding; each video keeps at least one model
rating), fixes each rater's video order and the sample for the closing
"who wrote this?" question. The questions follow the variable's type
(:data:`QUESTIONS`). Practice videos open the task and are never analysed.
"""

import csv
import hashlib
import io
import random
import secrets
from collections import Counter

import numpy as np
import pandas as pd

import fyp.annotation.ab_eval as ab
import fyp.annotation.human_eval as he
import fyp.core.data_io as data_io
from fyp.core.runtime import cf as _cf

RATING = "rating"

EXCLUSION_RULES = ("none", "missed_swap")
GUESS_SAMPLE = 10  # at most; half human-written, half the model's
BOOTSTRAP_SAMPLES = 1000
GUESS_VALUES = ("person", "model", "not_sure")
MAX_TRAP_CHARS = 4000

# Rating questions per answer type. Option order is the ordinal order used for
# alpha; "cant_tell" is never ranked (it counts as no judgement).
QUESTIONS: dict[str, list[dict]] = {
    "text": [
        {
            "id": "accuracy",
            "label": "Accuracy",
            "options": [
                ["no_errors", "No errors"],
                ["minor_error", "Minor error"],
                ["major_error", "Major error"],
            ],
        },
        {
            "id": "main_point",
            "label": "Captures the main point",
            "options": [["yes", "Yes"], ["partly", "Partly"], ["no", "No"]],
        },
        {
            "id": "overall",
            "label": "Overall quality",
            "options": [["1", "1 poor"], ["2", "2"], ["3", "3"], ["4", "4"], ["5", "5 excellent"]],
        },
    ],
    "phrase": [
        {
            "id": "fit",
            "label": "Fits the video",
            "options": [
                ["yes", "Yes"],
                ["partly", "Partly"],
                ["no", "No"],
                ["cant_tell", "Can't tell"],
            ],
        }
    ],
    "category": [
        {
            "id": "verdict",
            "label": "This answer is",
            "options": [
                ["correct", "Correct"],
                ["defensible", "Defensible, but not the best"],
                ["wrong", "Wrong"],
                ["cant_tell", "Can't tell"],
            ],
        }
    ],
    "number": [
        {
            "id": "closeness",
            "label": "This value is",
            "options": [
                ["about_right", "About right"],
                ["somewhat_off", "Somewhat off"],
                ["far_off", "Far off"],
                ["cant_tell", "Can't tell"],
            ],
        }
    ],
    "list": [
        {
            "id": "missing",
            "label": "Is anything important missing from the list?",
            "options": [
                ["none", "Nothing important"],
                ["minor", "Something minor"],
                ["major", "Something important"],
            ],
        }
    ],
}
LIST_ITEM_OPTIONS = [["there", "There"], ["not_there", "Not there"], ["cant_tell", "Can't tell"]]
CANT_RATE_REASONS = [
    ["wont_play", "The video won't play"],
    ["rather_not", "I'd rather not watch it"],
    ["other", "Another reason"],
]

# Headline figures per answer type: (name, question, levels counted as 1).
# None as levels means the mean of the ranked answer (the 1-5 score); the
# list precision has its own statistic.
_HEADLINES: dict[str, list[tuple]] = {
    "text": [
        ("share_no_errors", "accuracy", {"no_errors"}),
        ("share_major_error", "accuracy", {"major_error"}),
        ("share_main_point", "main_point", {"yes"}),
        ("mean_overall", "overall", None),
    ],
    "phrase": [("share_fits", "fit", {"yes"}), ("share_does_not_fit", "fit", {"no"})],
    "category": [("share_correct", "verdict", {"correct"}), ("share_wrong", "verdict", {"wrong"})],
    "number": [
        ("share_about_right", "closeness", {"about_right"}),
        ("share_far_off", "closeness", {"far_off"}),
    ],
    "list": [
        ("item_precision", None, None),
        ("share_nothing_missing", "missing", {"none"}),
        ("share_important_missing", "missing", {"major"}),
    ],
}

# Display-only list entries that name no actual thing (merged / empty labels).
_LIST_NO_ITEM = {
    "",
    "none",
    "n/a",
    "-",
    "--",
    "<na>",
    "unable to detect",
    "not coded",
    "other category",
}


# ---------------------------------------------------------------------------
# Answer types and display.
# ---------------------------------------------------------------------------


def rate_kind(spec: dict) -> str:
    """The rating question set for a variable: text / phrase / category / list / number."""
    kind = spec.get("kind")
    if kind == "enum":
        return "phrase" if spec.get("open") else "category"
    return {"list": "list", "numeric": "number"}.get(kind, "text")


def _is_missing(value) -> bool:
    if isinstance(value, (list, tuple, np.ndarray)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _list_items(value) -> list[str] | None:
    """A list answer's entries as shown to raters, or ``None`` for no answer."""
    if _is_missing(value):
        return None
    if isinstance(value, (list, tuple, np.ndarray)):
        parts = [str(v) for v in value if not _is_missing(v)]
    else:
        splitter = _cf()["labels"]["SPLITTER"]
        text = str(value)
        parts = text.split(splitter) if splitter and splitter in text else [text]
    out: list[str] = []
    for part in (" ".join(p.split()) for p in parts):
        if he._fold(part) not in _LIST_NO_ITEM and part not in out:
            out.append(part)
    return out


def _display(value, spec: dict):
    """One stored answer in the form raters see, or ``None`` when there is none.

    Every source passes through here, so the format never gives a source
    away. Lists become their named entries (an empty list reads "nothing
    listed"); scores return to the codebook's scale (the database keeps 0-1);
    the merged "no answer" label of a category reads "can't tell / doesn't
    apply".
    """
    kind = rate_kind(spec)
    if kind == "list":
        return _list_items(value)
    if _is_missing(value):
        return None
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if np.isnan(number):
            return None
        value_range = spec.get("range")
        if value_range:
            low, high = float(value_range[0]), float(value_range[1])
            number = low + number * (high - low)
        return round(number, 1)
    text = " ".join(str(value).split())
    if not text:
        return None
    if kind in ("category", "phrase") and he._fold(text) in he._no_answer_labels():
        return "can't tell / doesn't apply"
    return text


def _same(a, b) -> bool:
    """True when two displayed answers say the same thing."""
    if isinstance(a, list) or isinstance(b, list):
        return {he._fold(x) for x in a or []} == {he._fold(x) for x in b or []}
    return he._fold(a) == he._fold(b)


# ---------------------------------------------------------------------------
# Draft: create, traps.
# ---------------------------------------------------------------------------


def _seeded(task: dict, *parts: str) -> random.Random:
    digest = hashlib.sha256("|".join([task["order_seed"], *parts]).encode()).hexdigest()
    return random.Random(int(digest, 16))


def _save(task: dict) -> None:
    with he._INDEX_LOCK:
        data_io.save_json(
            data=task,
            storage_location=ab.LOCATION,
            filename=he._human_file(task["run_id"], he._task_filename(RATING)),
        )
        he._update_tasks_index(he._index_entry(task))


def _submitted_coders(run_id: str) -> list[str]:
    """Coders who submitted the run's coding task (sources of human answers)."""
    coding = he.load_task(run_id, "coding")
    return sorted(
        u
        for u in (coding or {}).get("coders", {})
        if he.load_coder_state(run_id, "coding", u).get("status") == "submitted"
    )


def create_task(
    run_id: str,
    variables: list[str],
    raters: list[str],
    created_by: str = "",
    arm: str | None = None,
    coders_included: list[str] | None = None,
    n_videos: int | None = None,
    n_practice: int = 3,
    ratings_per_human: int = 2,
    swapped_per_rater: int = 3,
    exclusion_rule: str = "none",
) -> dict:
    """Create a rating task as a draft (nobody is invited until :func:`start`).

    Args:
        run_id: a finished run.
        variables: the variables whose answers are rated.
        raters: usernames who will rate (invited at start).
        created_by: audit actor.
        arm: the contract whose answers are evaluated (default: the run's
            first arm).
        coders_included: coders whose submitted coding supplies human-written
            answers (must have submitted the run's coding task).
        n_videos: rate a random sample of this many videos (default: all).
            Videos with human answers are always kept in the sample.
        n_practice: practice videos shown first and never analysed (drawn
            from videos outside the rated sample when there are any).
        ratings_per_human: how many raters judge each human answer (a target;
            each video keeps at least one rating of the model's answer).
        swapped_per_rater: swapped traps each rater gets, per variable.
        exclusion_rule: ``none`` or ``missed_swap`` (raters who miss a
            swapped trap are left out of the results), fixed now so it is
            set before any rating is seen.

    Returns:
        The stored draft.

    Raises:
        ValueError: on an invalid run, variable, arm, coder or setting.
    """
    run = ab.load_run(run_id)
    manifest = run.get("manifest") or {}
    if manifest.get("status") != "complete":
        raise ValueError(f"run {run_id} is not complete")
    if he.load_task(run_id, RATING) is not None:
        raise ValueError(f"run {run_id} already has a rating task — delete it first")
    arms = [a.get("name") for a in manifest.get("arms", []) if a.get("name")]
    arm = str(arm) if arm else (arms[0] if arms else "")
    if arm not in arms:
        raise ValueError(f"unknown contract: {arm}")
    if exclusion_rule not in EXCLUSION_RULES:
        raise ValueError(f"unknown exclusion rule: {exclusion_rule}")

    catalog = {v["name"]: v for v in he.available_variables(run_id)}
    variables = [str(v) for v in variables]
    if not variables:
        raise ValueError("select at least one variable")
    unknown = [v for v in variables if v not in catalog]
    if unknown:
        raise ValueError(f"unknown variables: {', '.join(unknown)}")

    coders_included = [str(c) for c in coders_included or []]
    submitted = set(_submitted_coders(run_id))
    not_submitted = [c for c in coders_included if c not in submitted]
    if not_submitted:
        raise ValueError(f"not submitted on this run's coding task: {', '.join(not_submitted)}")

    item_ids = [str(i) for i in manifest.get("item_ids", [])]
    if not item_ids:
        raise ValueError(f"run {run_id} has no items")
    task = {"order_seed": secrets.token_hex(16)}

    # Videos with human answers stay in the rated sample; practice videos come
    # from outside it where possible, so no rater sees a video twice.
    coded = set()
    coding = he.load_task(run_id, "coding")
    for coder in coders_included:
        state = he.load_coder_state(run_id, "coding", coder)
        coded |= {i for i, r in (state.get("responses") or {}).items() if r.get("values")}
    coded &= set(item_ids)
    rng = _seeded(task, "sample")
    rest = [i for i in item_ids if i not in coded]
    rng.shuffle(rest)
    n_rated = len(item_ids) if not n_videos else max(int(n_videos), len(coded))
    n_rated = min(n_rated, len(item_ids))
    rated = sorted(coded) + rest[: max(n_rated - len(coded), 0)]
    outside = rest[max(n_rated - len(coded), 0) :]
    n_practice = max(int(n_practice or 0), 0)
    practice = outside[:n_practice]
    if len(practice) < n_practice:  # no spare videos: take uncoded ones from the sample
        spare = [i for i in rated if i not in coded][: n_practice - len(practice)]
        practice += spare
        rated = [i for i in rated if i not in spare]
    rated_set, practice_set = set(rated), set(practice)
    ordered_rated = [i for i in item_ids if i in rated_set]
    ordered_practice = [i for i in item_ids if i in practice_set]

    items = {
        str(item["item_id"]): {"item_id": str(item["item_id"]), "platform": item["platform"]}
        for item in ab.resolve_items(ordered_practice + ordered_rated)
    }
    now = he._now_iso()
    task.update(
        {
            "run_id": run_id,
            "task_type": RATING,
            "status": "draft",
            "created_at": now,
            "created_by": created_by,
            "arm": arm,
            "coders_included": coders_included,
            "practice_item_ids": ordered_practice,
            "rated_item_ids": ordered_rated,
            "item_ids": ordered_practice + ordered_rated,
            "items": [items[i] for i in ordered_practice + ordered_rated if i in items],
            "variables": variables,
            "field_specs": {
                v: {
                    "scale": he._kind_to_scale(catalog[v]["kind"]),
                    "kind": catalog[v]["kind"],
                    "label": catalog[v]["label"],
                    "description": catalog[v]["description"],
                    "values": catalog[v]["values"],
                    **{k: catalog[v].get(k) for k in he._OPTION_KEYS},
                    "rate_kind": rate_kind(catalog[v]),
                }
                for v in variables
            },
            "coders": {
                str(u): {"invited_at": None, "invited_by": created_by, "notified": False}
                for u in dict.fromkeys(str(u) for u in raters)
            },
            "settings": {
                "ratings_per_human": max(int(ratings_per_human or 0), 0),
                "swapped_per_rater": max(int(swapped_per_rater or 0), 0),
                "exclusion_rule": exclusion_rule,
            },
            "traps": [],
        }
    )
    _save(task)
    return task


def _load_draft(run_id: str) -> dict:
    task = he.load_task(run_id, RATING)
    if task is None:
        raise ValueError(f"run {run_id} has no rating task")
    if task.get("status") != "draft":
        raise ValueError("the rating task has started; its answers are fixed")
    return task


def _model_answers(task: dict, item_ids: list[str]) -> dict[tuple[str, str], object]:
    """``{(item_id, variable): displayed answer}`` for the evaluated contract."""
    variables = task["variables"]
    frame = he._arm_frame(task["run_id"], task["arm"], [*variables, "annotated_ok"])
    out: dict[tuple[str, str], object] = {}
    if frame is None:
        return out
    wanted = set(item_ids)
    for row in frame.drop_duplicates("item_id").to_dict("records"):
        item_id = str(row["item_id"])
        if item_id not in wanted:
            continue
        if (
            "annotated_ok" in row
            and not _is_missing(row["annotated_ok"])
            and not row["annotated_ok"]
        ):
            continue  # the contract failed this video: there is nothing to rate
        for variable in variables:
            shown = _display(row.get(variable), task["field_specs"][variable])
            if shown is not None:
                out[(item_id, variable)] = shown
    return out


def model_answers_for(run_id: str, item_id: str) -> dict:
    """The contract's answers for one video, as raters would see them.

    Used by the trap editor to pre-fill a copy for the researcher to edit.
    """
    task = he.load_task(run_id, RATING)
    if task is None:
        raise ValueError(f"run {run_id} has no rating task")
    answers = _model_answers(task, [str(item_id)])
    return {v: answers.get((str(item_id), v)) for v in task["variables"]}


def _clean_trap_value(value, spec: dict):
    kind = rate_kind(spec)
    if kind == "list":
        items = value if isinstance(value, list) else str(value or "").split("\n")
        return [" ".join(str(v).split()) for v in items if str(v).strip()][:200]
    if kind == "number":
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            raise ValueError("a number trap needs a number") from None
    text = " ".join(str(value or "").split())[:MAX_TRAP_CHARS]
    if not text:
        raise ValueError("the trap answer is empty")
    return text


def add_trap(
    run_id: str, item_id: str, variable: str, value, note: str = "", created_by: str = ""
) -> dict:
    """Add a hand-written trap to a draft: an edited copy of a model answer.

    One trap per (video, variable); writing another replaces it.

    Returns:
        The stored trap.
    """
    task = _load_draft(run_id)
    item_id, variable = str(item_id), str(variable)
    if item_id not in task["rated_item_ids"]:
        raise ValueError("pick one of the task's rated videos")
    if variable not in task["variables"]:
        raise ValueError(f"{variable} is not rated in this task")
    trap = {
        "trap_id": hashlib.sha256(
            f"{task['order_seed']}|{item_id}|{variable}".encode()
        ).hexdigest()[:10],
        "item_id": item_id,
        "variable": variable,
        "value": _clean_trap_value(value, task["field_specs"][variable]),
        "note": str(note or "").strip()[: he.MAX_NOTE_CHARS],
        "created_at": he._now_iso(),
        "created_by": created_by,
    }
    task["traps"] = [t for t in task["traps"] if t["trap_id"] != trap["trap_id"]] + [trap]
    _save(task)
    return trap


def delete_trap(run_id: str, trap_id: str) -> bool:
    """Remove a hand-written trap from a draft."""
    task = _load_draft(run_id)
    before = len(task["traps"])
    task["traps"] = [t for t in task["traps"] if t["trap_id"] != str(trap_id)]
    if len(task["traps"]) == before:
        return False
    _save(task)
    return True


# ---------------------------------------------------------------------------
# Start: freeze the answers and plan who sees what.
# ---------------------------------------------------------------------------


def _human_answers(task: dict) -> dict[tuple[str, str], dict[str, object]]:
    """``{(item_id, variable): {coder: displayed answer}}`` from the coding task."""
    coding = he.load_task(task["run_id"], "coding")
    out: dict[tuple[str, str], dict[str, object]] = {}
    if coding is None:
        return out
    shared = [v for v in task["variables"] if v in coding.get("variables", [])]
    rated = set(task["rated_item_ids"])
    for coder in task.get("coders_included", []):
        state = he.load_coder_state(task["run_id"], "coding", coder)
        if state.get("status") != "submitted":
            continue
        frame = he._database_frame(coding, state)
        for row in frame.to_dict("records"):
            item_id = str(row["item_id"])
            if item_id not in rated:
                continue
            for variable in shared:
                shown = _display(row.get(variable), task["field_specs"][variable])
                if shown is not None:
                    out.setdefault((item_id, variable), {})[coder] = shown
    return out


def source_group(source: str) -> str:
    """``model`` / ``human`` / ``swap`` / ``trap`` for a stored answer source."""
    return source.split(":", 1)[0]


def start(run_id: str, started_by: str = "") -> dict:
    """Freeze a draft's answers, assign them to raters, and open the task.

    Per variable: hand-written traps go out first (each to one rater, the
    least-trapped), then human answers (each to up to ``ratings_per_human``
    raters other than its author, the least-loaded first), then each rater's
    swapped traps, and the model's answer fills every remaining slot. A
    video always keeps one slot for the model's answer when there are two or
    more raters. Ties break on a hash of the task's seed, so the plan is
    reproducible.

    Returns:
        A summary: per rater, how many answers of each source group they got;
        per source group, how many answers and ratings; and warnings.
    """
    task = _load_draft(run_id)
    raters = sorted(task["coders"])
    if not raters:
        raise ValueError("add at least one rater before starting")
    variables = task["variables"]
    rated = task["rated_item_ids"]
    practice = task["practice_item_ids"]
    settings = task["settings"]
    model = _model_answers(task, practice + rated)
    humans = _human_answers(task)

    answers: dict[str, dict] = {}

    def _answer(item_id: str, variable: str, source: str, value, **extra) -> str:
        key = f"{task['order_seed']}|{item_id}|{variable}|{source}"
        answer_id = hashlib.sha256(key.encode()).hexdigest()[:12]
        answers.setdefault(
            answer_id,
            {"item_id": item_id, "variable": variable, "source": source, "value": value, **extra},
        )
        return answer_id

    assignment: dict[str, dict[str, dict[str, str]]] = {r: {} for r in raters}
    warnings: list[str] = []
    for variable in variables:
        rng = _seeded(task, "plan", variable)
        taken: dict[str, set] = {i: set() for i in rated}

        def _spare(item_id: str, variable: str = variable, taken: dict = taken) -> int:
            reserve = 1 if len(raters) > 1 and (item_id, variable) in model else 0
            return len(raters) - reserve - len(taken[item_id])

        def _give(
            rater: str, item_id: str, answer_id: str, variable: str = variable, taken: dict = taken
        ) -> None:
            assignment[rater].setdefault(item_id, {})[variable] = answer_id
            taken[item_id].add(rater)

        trap_load: Counter = Counter()
        traps = sorted(
            (t for t in task["traps"] if t["variable"] == variable), key=lambda t: t["trap_id"]
        )
        rng.shuffle(traps)
        for trap in traps:
            item_id = trap["item_id"]
            candidates = [r for r in raters if r not in taken[item_id]]
            if not candidates or _spare(item_id) <= 0:
                warnings.append(f"trap on {item_id}/{variable} not shown: no free rater")
                continue
            rater = min(candidates, key=lambda r: (trap_load[r], rng.random()))
            _give(
                rater,
                item_id,
                _answer(
                    item_id,
                    variable,
                    f"trap:{trap['trap_id']}",
                    trap["value"],
                    note=trap.get("note", ""),
                ),
            )
            trap_load[rater] += 1

        human_load: Counter = Counter()
        written = sorted(
            (item_id, coder, value)
            for (item_id, var), by_coder in humans.items()
            if var == variable
            for coder, value in by_coder.items()
        )
        rng.shuffle(written)
        for item_id, coder, value in written:
            answer_id = _answer(item_id, variable, f"human:{coder}", value)
            for _ in range(settings["ratings_per_human"]):
                candidates = [r for r in raters if r != coder and r not in taken[item_id]]
                if not candidates or _spare(item_id) <= 0:
                    warnings.append(
                        f"human answer {coder} on {item_id}/{variable}: fewer raters than asked for"
                    )
                    break
                rater = min(candidates, key=lambda r: (human_load[r], rng.random()))
                _give(rater, item_id, answer_id)
                human_load[rater] += 1

        donors = [i for i in rated if (i, variable) in model]
        for rater in sorted(raters, key=lambda r: rng.random()):
            open_items = [i for i in donors if rater not in taken[i] and _spare(i) > 0]
            rng.shuffle(open_items)
            placed = 0
            for item_id in open_items:
                if placed >= settings["swapped_per_rater"]:
                    break
                others = [
                    j
                    for j in donors
                    if j != item_id and not _same(model[(j, variable)], model[(item_id, variable)])
                ]
                if not others:
                    continue
                donor = rng.choice(others)
                _give(
                    rater,
                    item_id,
                    _answer(
                        item_id,
                        variable,
                        f"swap:{rater}",
                        model[(donor, variable)],
                        swapped_from=donor,
                    ),
                )
                placed += 1
            if placed < settings["swapped_per_rater"]:
                warnings.append(f"{rater}: only {placed} swapped traps for {variable}")

        for item_id in rated:
            if (item_id, variable) not in model:
                continue
            answer_id = _answer(item_id, variable, "model", model[(item_id, variable)])
            for rater in raters:
                if rater not in taken[item_id]:
                    _give(rater, item_id, answer_id)

    for item_id in practice:
        for variable in variables:
            if (item_id, variable) in model:
                answer_id = _answer(
                    item_id, variable, "model", model[(item_id, variable)], practice=True
                )
                for rater in raters:
                    assignment[rater].setdefault(item_id, {})[variable] = answer_id

    order: dict[str, list[str]] = {}
    guess_sample: dict[str, list[str]] = {}
    for rater in raters:
        mine = [i for i in rated if i in assignment[rater]]
        _seeded(task, "order", rater).shuffle(mine)
        order[rater] = [i for i in practice if i in assignment[rater]] + mine
        candidates = [
            answer_id
            for item_id in mine
            for variable, answer_id in assignment[rater][item_id].items()
            if task["field_specs"][variable]["rate_kind"] in ("text", "phrase")
            and source_group(answers[answer_id]["source"]) in ("model", "human")
        ]
        # Balanced: as many human as model answers, so chance is 50%. Traps
        # never appear; without human answers there is nothing to guess.
        rng = _seeded(task, "guess", rater)
        by_group = {
            g: sorted(a for a in candidates if source_group(answers[a]["source"]) == g)
            for g in ("human", "model")
        }
        k = min(len(by_group["human"]), len(by_group["model"]), GUESS_SAMPLE // 2)
        picked = rng.sample(by_group["human"], k) + rng.sample(by_group["model"], k)
        rng.shuffle(picked)
        guess_sample[rater] = picked

    now = he._now_iso()
    task.update(
        {
            "status": "started",
            "started_at": now,
            "started_by": started_by,
            "answers": answers,
            "assignment": assignment,
            "order": order,
            "guess_sample": guess_sample,
        }
    )
    for meta in task["coders"].values():
        meta["invited_at"] = meta.get("invited_at") or now
    _save(task)
    return plan_summary(task, warnings)


def plan_summary(task: dict, warnings: list[str] | None = None) -> dict:
    """Counts behind a started task's plan, for the admin and the report."""
    answers = task.get("answers", {})
    per_rater: dict[str, Counter] = {}
    ratings_per_answer: Counter = Counter()
    practice = set(task.get("practice_item_ids", []))
    for rater, items in task.get("assignment", {}).items():
        counts: Counter = Counter()
        for item_id, by_variable in items.items():
            if item_id in practice:
                continue
            for answer_id in by_variable.values():
                counts[source_group(answers[answer_id]["source"])] += 1
                ratings_per_answer[answer_id] += 1
        per_rater[rater] = counts
    groups: dict[str, dict] = {}
    for answer_id, n in ratings_per_answer.items():
        g = groups.setdefault(
            source_group(answers[answer_id]["source"]), {"answers": 0, "ratings": 0}
        )
        g["answers"] += 1
        g["ratings"] += n
    return {
        "per_rater": {r: dict(c) for r, c in per_rater.items()},
        "by_source": groups,
        "warnings": sorted(set(warnings or [])),
    }


# ---------------------------------------------------------------------------
# Rater side.
# ---------------------------------------------------------------------------


def rater_payload(task: dict, username: str, state: dict) -> dict:
    """What one rater's browser receives: never a source, note or other rater.

    Items come in the rater's own order (practice first); each carries the one
    answer per variable assigned to this rater. The closing guess sample
    carries opaque keys.
    """
    order = task.get("order", {}).get(username)
    if order is None:
        return {"items": [], "not_a_rater": True}
    answers = task.get("answers", {})
    platforms = {i["item_id"]: i.get("platform") for i in task.get("items", [])}
    practice = set(task.get("practice_item_ids", []))
    mine = task["assignment"][username]
    items = [
        {
            "item_id": item_id,
            "platform": platforms.get(item_id),
            "practice": item_id in practice,
            "answers": {
                variable: answers[answer_id]["value"]
                for variable, answer_id in mine.get(item_id, {}).items()
            },
        }
        for item_id in order
    ]
    guesses = [
        {
            "key": answer_id,
            "variable": answers[answer_id]["variable"],
            "value": answers[answer_id]["value"],
        }
        for answer_id in task.get("guess_sample", {}).get(username, [])
    ]
    return {
        "items": items,
        "questions": QUESTIONS,
        "list_item_options": LIST_ITEM_OPTIONS,
        "cant_rate_reasons": CANT_RATE_REASONS,
        "guesses": guesses,
        "my_guesses": state.get("guesses") or {},
    }


def validate_rating(task: dict, username: str, item_id: str, values: dict) -> dict:
    """Validate one video's ratings against the rater's own assignment.

    ``values`` is ``{"cant_rate": bool, "cant_rate_reason": str,
    "ratings": {variable: {question: option, "items": {entry: option}}}}``.
    Unanswered questions are simply absent.

    Raises:
        ValueError: on a video not assigned to the rater or an invalid option.
    """
    if task.get("status") != "started":
        raise ValueError("this rating task has not started")
    assigned = (task.get("assignment", {}).get(username) or {}).get(str(item_id))
    if assigned is None:
        raise ValueError("this video is not assigned to you")
    values = values or {}
    if values.get("cant_rate"):
        reason = str(values.get("cant_rate_reason") or "")
        if reason and reason not in {r for r, _ in CANT_RATE_REASONS}:
            raise ValueError(f"unknown reason: {reason}")
        return {"cant_rate": True, "cant_rate_reason": reason, "ratings": {}}
    cleaned: dict[str, dict] = {}
    for variable, rating in (values.get("ratings") or {}).items():
        if variable not in assigned:
            raise ValueError(f"{variable} is not rated for this video")
        kind = task["field_specs"][variable]["rate_kind"]
        out: dict = {}
        for question in QUESTIONS[kind]:
            choice = (rating or {}).get(question["id"])
            if choice in (None, ""):
                continue
            if str(choice) not in {o for o, _ in question["options"]}:
                raise ValueError(f"{variable}: invalid {question['id']}: {choice!r}")
            out[question["id"]] = str(choice)
        if kind == "list":
            shown = task["answers"][assigned[variable]]["value"] or []
            marks = {}
            for entry, choice in ((rating or {}).get("items") or {}).items():
                if entry not in shown:
                    raise ValueError(f"{variable}: {entry!r} is not in the list shown")
                if choice in (None, ""):
                    continue
                if choice not in {o for o, _ in LIST_ITEM_OPTIONS}:
                    raise ValueError(f"{variable}: invalid mark {choice!r}")
                marks[entry] = choice
            if marks:
                out["items"] = marks
        if out:
            cleaned[variable] = out
    # Nothing rated yet (e.g. a note before the answers were shown): store no
    # values, so the video does not count as done.
    return {"cant_rate": False, "ratings": cleaned} if cleaned else {}


def save_guesses(run_id: str, username: str, guesses: dict) -> dict:
    """Save a rater's closing "who wrote this?" guesses (before submitting)."""
    task = he.load_task(run_id, RATING)
    if task is None or task.get("status") != "started":
        raise ValueError("this rating task is not open")
    sample = set(task.get("guess_sample", {}).get(username, []))
    cleaned = {}
    for key, value in (guesses or {}).items():
        if key not in sample:
            raise ValueError("unknown answer")
        if value not in GUESS_VALUES:
            raise ValueError(f"invalid guess: {value!r}")
        cleaned[key] = value
    state = he.load_coder_state(run_id, RATING, username)
    if state.get("status") == "submitted":
        raise ValueError("this rating has already been submitted")
    state["guesses"] = {**(state.get("guesses") or {}), **cleaned}
    state["updated_at"] = he._now_iso()
    data_io.save_json(
        data=state,
        storage_location=ab.LOCATION,
        filename=he._human_file(run_id, he._coder_filename(RATING, username)),
    )
    return state


# ---------------------------------------------------------------------------
# Results.
# ---------------------------------------------------------------------------


def _caught(kind: str, rating: dict, answer: dict, model_value) -> bool | None:
    """Did a rater flag a trap? ``None`` when there is no judgement to count.

    A swapped answer is about another video, so only a clear rejection
    counts; a hand-written trap holds one planted error, so flagging any
    error counts.
    """
    swapped = source_group(answer["source"]) == "swap"
    if kind == "text":
        accuracy, point = rating.get("accuracy"), rating.get("main_point")
        if accuracy is None and point is None:
            return None
        if swapped:
            return accuracy == "major_error" or point == "no"
        return accuracy in ("minor_error", "major_error") or point in ("partly", "no")
    if kind == "phrase":
        fit = rating.get("fit")
        if fit in (None, "cant_tell"):
            return None
        return fit == "no" if swapped else fit in ("partly", "no")
    if kind == "category":
        verdict = rating.get("verdict")
        return None if verdict in (None, "cant_tell") else verdict == "wrong"
    if kind == "number":
        closeness = rating.get("closeness")
        if closeness in (None, "cant_tell"):
            return None
        return closeness in ("somewhat_off", "far_off")
    marks = rating.get("items") or {}
    judged = [m for m in marks.values() if m in ("there", "not_there")]
    if not judged and rating.get("missing") is None:
        return None
    if swapped:
        return bool(judged) and sum(m == "not_there" for m in judged) / len(judged) >= 0.5
    shown = {he._fold(x): x for x in answer["value"] or []}
    original = {he._fold(x) for x in model_value or []}
    added = [shown[k] for k in shown if k not in original]
    removed = bool(original - set(shown))
    return any(marks.get(x) == "not_there" for x in added) or (
        removed and rating.get("missing") in ("minor", "major")
    )


def _headline(kind: str, name: str, question: str | None, levels: set | None, ratings: list[dict]):
    """One headline figure over a set of ratings (``None`` without data)."""
    if name == "item_precision":
        there = not_there = 0
        for r in ratings:
            marks = (r.get("items") or {}).values()
            there += sum(m == "there" for m in marks)
            not_there += sum(m == "not_there" for m in marks)
        return there / (there + not_there) if there + not_there else None
    answered = [r.get(question) for r in ratings if r.get(question) not in (None, "cant_tell")]
    if not answered:
        return None
    if levels is None:
        return float(np.mean([int(v) for v in answered]))
    return sum(v in levels for v in answered) / len(answered)


def _bootstrap(rows: list[dict], statistic, seed: str) -> tuple[float | None, float | None]:
    """95% interval for ``statistic(rows)``, resampling whole videos."""
    by_video: dict[str, list[dict]] = {}
    for row in rows:
        by_video.setdefault(row["item_id"], []).append(row)
    videos = sorted(by_video)
    if len(videos) < 2:
        return None, None
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest(), 16))
    values = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sample = [r for v in rng.choices(videos, k=len(videos)) for r in by_video[v]]
        value = statistic(sample)
        if value is not None:
            values.append(value)
    if len(values) < BOOTSTRAP_SAMPLES // 2:
        return None, None
    return float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))


def _agreement(rows: list[dict], question: str, options: list[str]) -> dict:
    """Ordinal alpha + pairwise percent agreement over answers rated twice or more."""
    ranked = [o for o in options if o != "cant_tell"]
    by_answer: dict[str, list] = {}
    for row in rows:
        value = row["rating"].get(question)
        if value in ranked:
            by_answer.setdefault(row["answer_id"], []).append(ranked.index(value))
    units = [v for v in by_answer.values() if len(v) >= 2]
    pairs = agree = 0
    for unit in units:
        for a in range(len(unit)):
            for b in range(a + 1, len(unit)):
                pairs += 1
                agree += unit[a] == unit[b]
    alpha = he.krippendorff_alpha(units, "ordinal") if units else {"alpha": None, "n_units": 0}
    return {
        "alpha": alpha["alpha"],
        "pct_agreement": agree / pairs if pairs else None,
        "n_units": len(units),
    }


def _list_item_agreement(rows: list[dict]) -> dict:
    """Agreement on "there / not there" for list entries judged by two raters or more."""
    by_entry: dict[tuple, list] = {}
    for row in rows:
        for entry, mark in (row["rating"].get("items") or {}).items():
            if mark in ("there", "not_there"):
                by_entry.setdefault((row["answer_id"], entry), []).append(mark)
    units = [v for v in by_entry.values() if len(v) >= 2]
    pairs = sum(len(u) * (len(u) - 1) // 2 for u in units)
    agree = sum(
        sum(u[a] == u[b] for a in range(len(u)) for b in range(a + 1, len(u))) for u in units
    )
    alpha = he.krippendorff_alpha(units, "nominal") if units else {"alpha": None}
    return {
        "alpha": alpha["alpha"],
        "pct_agreement": agree / pairs if pairs else None,
        "n_units": len(units),
    }


def _rating_rows(task: dict, states: dict[str, dict]) -> list[dict]:
    """One row per (rater, video, variable) judgement on a rated video."""
    answers = task.get("answers", {})
    practice = set(task.get("practice_item_ids", []))
    rows = []
    for rater, state in states.items():
        responses = state.get("responses") or {}
        for item_id, by_variable in (task.get("assignment", {}).get(rater) or {}).items():
            values = (responses.get(item_id) or {}).get("values") or {}
            for variable, answer_id in by_variable.items():
                rows.append(
                    {
                        "rater": rater,
                        "item_id": item_id,
                        "variable": variable,
                        "answer_id": answer_id,
                        "group": source_group(answers[answer_id]["source"]),
                        "practice": item_id in practice,
                        "cant_rate": bool(values.get("cant_rate")),
                        "answered": item_id in responses,
                        "rating": (values.get("ratings") or {}).get(variable) or {},
                    }
                )
    return rows


def compute_results(run_id: str, task: dict) -> dict:
    """Score a started rating task from the submitted raters and persist it.

    Per variable and source group: headline shares (or the mean 1-5 score)
    with 95% intervals from resampling videos, response distributions, and
    rater agreement (ordinal alpha + percent agreement) on answers judged two
    or more times. Model against human: the per-video difference on each
    headline over videos with both. Traps: catch rate per rater, which also
    drives the exclusion rule. Guesses: share correct against the true
    source. Practice videos and "can't rate" videos are left out.
    """
    states = {
        rater: state
        for rater in task.get("coders", {})
        if (state := he.load_coder_state(run_id, RATING, rater)).get("status") == "submitted"
    }
    answers = task.get("answers", {})
    rows = [
        r
        for r in _rating_rows(task, states)
        if not r["practice"] and not r["cant_rate"] and r["rating"]
    ]
    model = {
        (a["item_id"], a["variable"]): a["value"]
        for a in answers.values()
        if a["source"] == "model"
    }

    raters: dict[str, dict] = {}
    for rater in states:
        mine = [r for r in rows if r["rater"] == rater]
        swaps = traps = swaps_caught = traps_caught = 0
        for row in mine:
            if row["group"] not in ("swap", "trap"):
                continue
            kind = task["field_specs"][row["variable"]]["rate_kind"]
            caught = _caught(
                kind,
                row["rating"],
                answers[row["answer_id"]],
                model.get((row["item_id"], row["variable"])),
            )
            if caught is None:
                continue
            if row["group"] == "swap":
                swaps += 1
                swaps_caught += caught
            else:
                traps += 1
                traps_caught += caught
        cant_rate = sum(
            1
            for item_id, response in (states[rater].get("responses") or {}).items()
            if (response.get("values") or {}).get("cant_rate")
            and item_id not in task.get("practice_item_ids", [])
        )
        raters[rater] = {
            "n_ratings": len(mine),
            "n_cant_rate": cant_rate,
            "swaps": swaps,
            "swaps_caught": swaps_caught,
            "traps": traps,
            "traps_caught": traps_caught,
            "excluded": task["settings"]["exclusion_rule"] == "missed_swap"
            and swaps_caught < swaps,
        }
    excluded = sorted(r for r, info in raters.items() if info["excluded"])
    kept = [r for r in rows if r["rater"] not in excluded]

    variables: dict[str, dict] = {}
    for variable in task["variables"]:
        kind = task["field_specs"][variable]["rate_kind"]
        entry: dict = {
            "rate_kind": kind,
            "label": task["field_specs"][variable]["label"],
            "by_source": {},
            "agreement": {},
        }
        for group in ("model", "human", "swap", "trap"):
            group_rows = [r for r in kept if r["variable"] == variable and r["group"] == group]
            if not group_rows:
                continue
            ratings = [r["rating"] for r in group_rows]
            headline = {}
            for name, question, levels in _HEADLINES[kind]:

                def statistic(sample, kind=kind, name=name, question=question, levels=levels):
                    return _headline(kind, name, question, levels, [r["rating"] for r in sample])

                value = statistic(group_rows)
                low, high = _bootstrap(
                    group_rows, statistic, f"{task['order_seed']}|{variable}|{group}|{name}"
                )
                headline[name] = {"value": value, "ci_low": low, "ci_high": high}
            distributions = {
                q["id"]: {o: sum(r.get(q["id"]) == o for r in ratings) for o, _ in q["options"]}
                for q in QUESTIONS[kind]
            }
            entry["by_source"][group] = {
                "n_answers": len({r["answer_id"] for r in group_rows}),
                "n_ratings": len(group_rows),
                "n_videos": len({r["item_id"] for r in group_rows}),
                "headline": headline,
                "distributions": distributions,
            }
            if group in ("model", "human"):
                agreement = {
                    q["id"]: _agreement(group_rows, q["id"], [o for o, _ in q["options"]])
                    for q in QUESTIONS[kind]
                }
                if kind == "list":
                    agreement["items"] = _list_item_agreement(group_rows)
                entry["agreement"][group] = agreement
        entry["model_vs_human"] = _model_vs_human(task, kind, variable, kept)
        variables[variable] = entry

    results = {
        "computed_at": he._now_iso(),
        "raters": raters,
        "excluded_raters": excluded,
        "exclusion_rule": task["settings"]["exclusion_rule"],
        "variables": variables,
        "guesses": _guess_results(task, states),
        "plan": plan_summary(task),
    }
    data_io.save_json(
        data=results,
        storage_location=ab.LOCATION,
        filename=he._human_file(run_id, he._results_filename(RATING)),
    )
    return results


def _model_vs_human(task: dict, kind: str, variable: str, rows: list[dict]) -> dict:
    """Model minus human on each headline, per video, over videos with both."""
    rows = [r for r in rows if r["variable"] == variable and r["group"] in ("model", "human")]
    both = {
        item_id
        for item_id in {r["item_id"] for r in rows}
        if {r["group"] for r in rows if r["item_id"] == item_id} == {"model", "human"}
    }
    if not both:
        return {}
    paired = [r for r in rows if r["item_id"] in both]
    out = {}
    for name, question, levels in _HEADLINES[kind]:

        def difference(sample, name=name, question=question, levels=levels):
            per_video = []
            for item_id in {r["item_id"] for r in sample}:
                of = [r for r in sample if r["item_id"] == item_id]
                m = _headline(
                    kind, name, question, levels, [r["rating"] for r in of if r["group"] == "model"]
                )
                h = _headline(
                    kind, name, question, levels, [r["rating"] for r in of if r["group"] == "human"]
                )
                if m is not None and h is not None:
                    per_video.append(m - h)
            return float(np.mean(per_video)) if per_video else None

        low, high = _bootstrap(paired, difference, f"{task['order_seed']}|{variable}|diff|{name}")
        out[name] = {
            "difference": difference(paired),
            "ci_low": low,
            "ci_high": high,
            "n_videos": len(both),
        }
    return out


def _guess_results(task: dict, states: dict[str, dict]) -> dict:
    """How well raters told human from model answers, against the truth."""
    answers = task.get("answers", {})
    by_rater, pooled = {}, Counter()
    for rater, state in states.items():
        counts: Counter = Counter()
        for answer_id, guess in (state.get("guesses") or {}).items():
            truth = (
                "person"
                if source_group(answers.get(answer_id, {}).get("source", "")) == "human"
                else "model"
            )
            counts["n"] += 1
            if guess == "not_sure":
                counts["not_sure"] += 1
            else:
                counts["decided"] += 1
                counts["correct"] += guess == truth
                counts[f"decided_{truth}"] += 1
                counts[f"correct_{truth}"] += guess == truth
        by_rater[rater] = dict(counts)
        pooled.update(counts)
    pooled = dict(pooled)
    return {
        "by_rater": by_rater,
        "pooled": pooled,
        "share_correct": pooled.get("correct", 0) / pooled["decided"]
        if pooled.get("decided")
        else None,
        "share_not_sure": pooled.get("not_sure", 0) / pooled["n"] if pooled.get("n") else None,
    }


# ---------------------------------------------------------------------------
# Export.
# ---------------------------------------------------------------------------


def export_csv(run_id: str) -> str:
    """Every rating as one row, with the answer judged and its true source.

    Long format for analysis (e.g. a mixed model with crossed rater and video
    effects): rater, video, practice flag, variable, answer type, source group
    and source, the answer as shown, the trap note or swap origin, each
    question's response, list-entry marks, "can't rate", the rater's source
    guess for that answer if asked, and whether the rater is excluded by the
    task's rule. Starts with a UTF-8 byte-order mark for spreadsheets.
    """
    task = he.load_task(run_id, RATING)
    if task is None or task.get("status") != "started":
        raise ValueError("the rating task has not started")
    answers = task["answers"]
    states = {r: he.load_coder_state(run_id, RATING, r) for r in task.get("coders", {})}
    results = he.load_results(run_id, RATING) or {}
    excluded = set(results.get("excluded_raters", []))
    platforms = {i["item_id"]: i.get("platform", "") for i in task.get("items", [])}
    questions = sorted({q["id"] for qs in QUESTIONS.values() for q in qs})
    header = [
        "rater", "rater_submitted", "rater_excluded", "item_id", "platform", "practice",
        "variable", "answer_type", "source_group", "source", "answer", "trap_note",
        "swapped_from", "cant_rate", "cant_rate_reason", *questions, "list_marks",
        "source_guess", "note",
    ]  # fmt: skip
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in _rating_rows(task, states):
        answer = answers[row["answer_id"]]
        response = (states[row["rater"]].get("responses") or {}).get(row["item_id"]) or {}
        values = response.get("values") or {}
        group = row["group"]
        source = answer["source"] if group in ("model", "human") else group
        if group == "model":
            source = f"model:{task['arm']}"
        marks = "; ".join(f"{k}: {v}" for k, v in (row["rating"].get("items") or {}).items())
        writer.writerow(
            [
                row["rater"],
                states[row["rater"]].get("status") == "submitted",
                row["rater"] in excluded,
                row["item_id"],
                platforms.get(row["item_id"], ""),
                row["practice"],
                row["variable"],
                task["field_specs"][row["variable"]]["rate_kind"],
                group,
                source,
                he._export_cell(answer["value"]),
                he._export_cell(answer.get("note", "")),
                answer.get("swapped_from", ""),
                row["cant_rate"],
                values.get("cant_rate_reason", ""),
                *[row["rating"].get(q, "") for q in questions],
                he._export_cell(marks),
                (states[row["rater"]].get("guesses") or {}).get(row["answer_id"], ""),
                he._export_cell(response.get("note", "")),
            ]
        )
    return "﻿" + buffer.getvalue()
