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

import argparse
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
        item_ids: optional explicit id list; defaults to the persisted set.

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

    reporter.update_progress(0, "Loading arms and eval set...")

    arms: list[dict] = []
    for name in names:
        cand = ab_eval.load_candidate(name)   # raises on missing/invalid → fail fast
        arms.append({"name": name, "source": "candidate", "text": cand["text"]})
    if include_live:
        arms.append({"name": "live", "source": "live", "text": ac.effective_contract_text()})
    if not arms:
        raise ValueError("no arms selected (pass candidate_names and/or include_live)")

    item_ids = task_args.get("item_ids") or ab_eval.load_eval_set().get("item_ids") or []
    if not item_ids:
        raise ValueError("the eval set is empty — curate it before starting a run")

    n_calls = len(item_ids) * len(arms)
    reporter.log(
        f"Run {run_id}: {len(arms)} arm(s) × {len(item_ids)} item(s) = {n_calls} Gemini calls."
    )
    reporter.emit_data({"run_id": run_id, "n_arms": len(arms),
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
        f"arms={','.join(summary['arms'])} items={summary['n_items']}"
    )
    reporter.log("Done. Open the A/B testing page to compare the arms.")
    return None




if __name__ == "__main__":
    from web_interface.task_status import LocalStatusReporter

    parser = argparse.ArgumentParser(description="A/B contract evaluation run")
    parser.add_argument("--run-id", default=None, help="pre-minted run id")
    parser.add_argument("--candidates", default="",
                        help="comma-separated candidate names")
    parser.add_argument("--include-live", action="store_true",
                        help="also run the live effective contract as an arm")
    cli = parser.parse_args()

    reporter = LocalStatusReporter("ab_eval")
    try:
        run_ab_eval(reporter=reporter, task_args={
            "run_id": cli.run_id,
            "candidate_names": cli.candidates,
            "include_live": cli.include_live,
        })
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
