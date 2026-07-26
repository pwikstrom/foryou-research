"""A/B contract-evaluation worker (Cloud Task / local subprocess).

Runs the candidate contracts named in ``task_args`` (plus optionally the live
contract) against the persisted eval set via ``fyp.ab_eval.execute_run``,
which snapshots each arm's contract text, annotates the set with real Gemini
calls, refines in memory through the production recode path, and stores the
per-arm results + comparison report under the isolated ``ab_eval`` location.

No self-chaining: the eval set is hard-capped at ``ab_eval.MAX_EVAL_ITEMS``
(50) items × a handful of arms, which finishes well inside the task-runner's
3600s timeout at the runner's concurrency. If arms/sets ever grow, chaining
one arm per task link is the fix.
"""

import sys
import time
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_ab_eval(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Execute one A/B evaluation run.

    task_args:
        run_id: pre-minted run id (the start endpoint mints it so the UI can
            follow the run immediately); a missing id is minted here.
        candidate_names: list (or comma-separated string) of candidate names.
        include_live: also run the live effective contract as an arm.
        eval_set: name of the evaluation set to use; defaults to the active one.
        item_ids: optional explicit id list; overrides the evaluation set.
        name: optional human-readable run label (shown in run pickers).

    Returns None (no chain).
    """
    from fyp import ab_eval
    from fyp import annotation_contract as ac

    task_args = task_args or {}
    _t_start = time.perf_counter()

    run_id = task_args.get("run_id") or ab_eval.new_run_id()
    names = task_args.get("candidate_names") or []
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    include_live = bool(task_args.get("include_live"))

    reporter.update_progress(0, "Loading candidate contracts and evaluation set...")

    arms: list[dict] = []
    arms_spec = task_args.get("arms_spec")
    if arms_spec:
        # Explicit arm list (route-validated): the same contract may appear as
        # several arms under distinct labels (e.g. once per backend). The
        # label is the arm's report/manifest key.
        live_text = None
        for spec in arms_spec:
            arm = {"name": spec["label"], "source": spec["source"]}
            if spec["source"] == "candidate":
                cand = ab_eval.load_candidate(spec["name"])  # raises → fail fast
                arm["text"] = cand["text"]
                arm["candidate"] = spec["name"]
            else:
                if live_text is None:
                    live_text = ac.effective_contract_text()
                arm["text"] = live_text
            if spec.get("backend"):
                arm["backend"] = spec["backend"]
            arms.append(arm)
    else:
        # Legacy shape: candidate name list + include_live + per-arm overrides
        # keyed by arm name ("live" for the live arm).
        arm_params = task_args.get("arm_params") or {}
        for name in names:
            cand = ab_eval.load_candidate(name)   # raises on missing/invalid → fail fast
            arms.append({"name": name, "source": "candidate", "text": cand["text"],
                         "candidate": name, **arm_params.get(name, {})})
        if include_live:
            arms.append({"name": "live", "source": "live", "text": ac.effective_contract_text(),
                         **arm_params.get("live", {})})
    if not arms:
        raise ValueError("no contracts selected (pass arms_spec, or candidate_names/include_live)")

    stored = ab_eval.load_eval_set(task_args.get("eval_set") or None)
    eval_set = stored.get("name") or ""
    item_ids = task_args.get("item_ids") or stored.get("item_ids") or []
    if not item_ids:
        raise ValueError("the evaluation set is empty — curate it before starting a run")

    n_calls = len(item_ids) * len(arms)
    reporter.log(
        f"Run {run_id}: {len(arms)} contract(s) × {len(item_ids)} item(s) from set "
        f"'{eval_set}' = {n_calls} annotation calls."
    )
    reporter.emit_data({"run_id": run_id, "n_arms": len(arms), "eval_set": eval_set,
                        "n_items": len(item_ids), "n_calls": n_calls})

    total_units = max(1, n_calls)
    done_units = {"n": 0}

    def _progress(arm_name: str, done: int, total: int) -> None:
        done_units["n"] += 1
        pct = int(round(done_units["n"] / total_units * 100))
        reporter.update_progress(min(pct, 99), f"{arm_name}: {done}/{total} items")

    try:
        summary = ab_eval.execute_run(
            run_id=run_id,
            arms=arms,
            item_ids=list(item_ids),
            started_by=str(task_args.get("started_by") or ""),
            eval_set=eval_set,
            name=str(task_args.get("name") or ""),
            progress_cb=_progress,
            cancel_cb=reporter.check_cancelled,
        )
    except ab_eval.RunCancelled:
        reporter.log(f"Run {run_id} cancelled; partial artifacts kept.")
        reporter.emit_data({"run_id": run_id, "status": "cancelled"})
        return None

    _t_total = time.perf_counter() - _t_start
    reporter.emit_data({"run_id": run_id, "status": summary["status"],
                        "costs": summary["costs"]})
    reporter.log(
        f"[TIMING] ab_eval total={_t_total:.2f}s run_id={run_id} "
        f"contracts={','.join(summary['arms'])} items={summary['n_items']}"
    )
    reporter.log("Done. Open the Annotation testing page to compare the contracts.")
    return None




if __name__ == "__main__":
    import json as _json

    from web_interface.worker_runner import run_worker

    run_worker(
        run_ab_eval,
        "ab_eval",
        arg_specs=[
            (("--run-id",), {"default": None, "help": "pre-minted run id"}),
            (("--candidates",), {"default": "",
                                 "help": "comma-separated candidate names"}),
            (("--include-live",), {"action": "store_true",
                                   "help": "also run the live effective contract as an arm"}),
            (("--eval-set",), {"default": None,
                               "help": "named test set (default: the active one)"}),
            (("--arms-spec",), {"default": None,
                                "help": "explicit arm list as JSON "
                                        "([{source, name?, label, backend?}, ...])"}),
            (("--name",), {"default": None, "help": "run name (shown in run pickers)"}),
            (("--started-by",), {"default": None, "help": "audit actor for the manifest"}),
        ],
        make_task_args=lambda cli: {
            "run_id": cli.run_id,
            "candidate_names": cli.candidates,
            "include_live": cli.include_live,
            "eval_set": cli.eval_set,
            "arms_spec": _json.loads(cli.arms_spec) if cli.arms_spec else None,
            "name": cli.name,
            "started_by": cli.started_by,
        },
        description="A/B contract test run",
    )
