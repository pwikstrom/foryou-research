"""A/B contract-evaluation worker (Cloud Task / local subprocess).

Runs the candidate contracts named in ``task_args`` (plus optionally the live
contract) against the persisted eval set via ``fyp.ab_eval.execute_run``,
which snapshots each arm's contract text, annotates the set with real Gemini
calls, refines in memory through the production recode path, and stores the
per-arm results + comparison report under the isolated ``ab_eval`` location.

Self-chaining: a test set can hold a few hundred videos (the limit is the
``[machine] test_set_max_items`` setting), and videos × contracts annotation
calls do not fit in one Cloud Task, whose dispatch deadline cannot exceed
1800 s. ``execute_run`` saves each contract's answers every few videos, so a
link annotates for ``LINK_BUDGET_SECONDS`` and then hands the rest to the next
link, which continues the same run from the run's own folder (its manifest and
stored contract texts, never the candidates as they are by then). The last
link writes the comparison report. A small run finishes in its first link,
exactly as before.
"""

import time

from web_interface.tasks import worker_registry
from web_interface.tasks.task_status import TaskStatusReporter

# How long one link keeps starting new chunks of videos. The chunk in flight
# when the budget runs out still finishes, and the last link also writes the
# report, so this sits well under the 1800 s dispatch deadline.
LINK_BUDGET_SECONDS = 900


def run_ab_eval(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Execute one A/B evaluation run, or one link of a long one.

    task_args:
        run_id: pre-minted run id (the start endpoint mints it so the UI can
            follow the run immediately); a missing id is minted here.
        candidate_names: list (or comma-separated string) of candidate names.
        include_live: also run the live effective contract as an arm.
        eval_set: name of the evaluation set to use; defaults to the active one.
        item_ids: optional explicit id list; overrides the evaluation set.
        name: optional human-readable run label (shown in run pickers).
        resume: set by a previous link — continue run ``run_id`` from its own
            folder; every other key above is then ignored.
        chunk_index: the link's position in the chain (0 for the first).

    Returns:
        ``None`` when the run is finished (or cancelled); a chain instruction
        when videos remain for the next link.
    """
    from fyp.annotation import ab_eval

    task_args = task_args or {}
    _t_start = time.perf_counter()

    resume = bool(task_args.get("resume"))
    link = int(task_args.get("chunk_index") or 0)
    run_id = task_args.get("run_id") or ab_eval.new_run_id()
    done_by_arm: dict[str, int] = {}

    if resume:
        manifest, arms = ab_eval.stored_arms(run_id)
        item_ids = manifest.get("item_ids") or []
        eval_set = manifest.get("eval_set") or ""
        run_name = manifest.get("name") or ""
        started_by = manifest.get("started_by") or ""
        done_by_arm = ab_eval.arm_progress(run_id, manifest)
        # Pick the bar up where the previous link left it, not at zero.
        done_so_far = sum(done_by_arm.values())
        reporter.update_progress(
            min(int(round(done_so_far / max(1, len(item_ids) * len(arms)) * 100)), 99),
            "Continuing the test run...",
        )
    else:
        reporter.update_progress(0, "Loading candidate contracts and evaluation set...")
        arms = _arms_from_task_args(task_args)
        stored = ab_eval.load_eval_set(task_args.get("eval_set") or None)
        eval_set = stored.get("name") or ""
        item_ids = task_args.get("item_ids") or stored.get("item_ids") or []
        if not item_ids:
            raise ValueError("the evaluation set is empty — curate it before starting a run")
        run_name = str(task_args.get("name") or "")
        started_by = str(task_args.get("started_by") or "")

    n_calls = len(item_ids) * len(arms)
    if resume:
        reporter.log(
            f"Run {run_id}: continuing ({sum(done_by_arm.values())} of {n_calls} "
            "annotation calls already made)."
        )
    else:
        reporter.log(
            f"Run {run_id}: {len(arms)} contract(s) × {len(item_ids)} item(s) from set "
            f"'{eval_set}' = {n_calls} annotation calls."
        )
    reporter.emit_data(
        {
            "run_id": run_id,
            "n_arms": len(arms),
            "eval_set": eval_set,
            "n_items": len(item_ids),
            "n_calls": n_calls,
        }
    )

    total_units = max(1, n_calls)

    def _progress(arm_name: str, done: int, total: int) -> None:
        done_by_arm[arm_name] = done
        pct = int(round(sum(done_by_arm.values()) / total_units * 100))
        reporter.update_progress(min(pct, 99), f"{arm_name}: {done}/{total} items")

    try:
        summary = ab_eval.execute_run(
            run_id=run_id,
            arms=arms,
            item_ids=list(item_ids),
            started_by=started_by,
            eval_set=eval_set,
            name=run_name,
            progress_cb=_progress,
            cancel_cb=reporter.check_cancelled,
            resume=resume,
            pause_cb=lambda: time.perf_counter() - _t_start > LINK_BUDGET_SECONDS,
        )
    except ab_eval.RunCancelled:
        reporter.log(f"Run {run_id} cancelled; partial artifacts kept.")
        reporter.emit_data({"run_id": run_id, "status": "cancelled"})
        return None

    _t_total = time.perf_counter() - _t_start
    if summary.get("paused"):
        done = sum(ab_eval.arm_progress(run_id).values())
        reporter.log(
            f"[TIMING] ab_eval link={link} total={_t_total:.2f}s run_id={run_id} "
            f"calls_done={done}/{n_calls}"
        )
        return {
            "chain": True,
            "next_task_args": {
                "run_id": run_id,
                "resume": True,
                "chunk_index": link + 1,
                "started_by": started_by,
            },
            "dispatch_deadline_seconds": worker_registry.deadline_for("ab_eval"),
            "chain_log_message": (
                f"{done} of {n_calls} annotation calls made; continuing in a new task."
            ),
        }

    reporter.emit_data({"run_id": run_id, "status": summary["status"], "costs": summary["costs"]})
    reporter.log(
        f"[TIMING] ab_eval total={_t_total:.2f}s run_id={run_id} link={link} "
        f"contracts={','.join(summary['arms'])} items={summary['n_items']}"
    )
    reporter.log("Done. Open the Annotation testing page to compare the contracts.")
    return None


def _arms_from_task_args(task_args: dict) -> list[dict]:
    """Build a new run's arms, snapshotting each contract's text now.

    Raises:
        ValueError: when no contract is selected (or a candidate is invalid).
    """
    from fyp.annotation import ab_eval
    from fyp.annotation import annotation_contract as ac

    names = task_args.get("candidate_names") or []
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    include_live = bool(task_args.get("include_live"))

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
            cand = ab_eval.load_candidate(name)  # raises on missing/invalid → fail fast
            arms.append(
                {
                    "name": name,
                    "source": "candidate",
                    "text": cand["text"],
                    "candidate": name,
                    **arm_params.get(name, {}),
                }
            )
        if include_live:
            arms.append(
                {
                    "name": "live",
                    "source": "live",
                    "text": ac.effective_contract_text(),
                    **arm_params.get("live", {}),
                }
            )
    if not arms:
        raise ValueError("no contracts selected (pass arms_spec, or candidate_names/include_live)")
    return arms


if __name__ == "__main__":
    import functools
    import json as _json

    from web_interface.tasks.worker_runner import chain_locally, run_worker

    run_worker(
        # A long run pauses between links; locally the next link runs in this
        # same process.
        functools.partial(chain_locally, run_ab_eval),
        "ab_eval",
        arg_specs=[
            (("--run-id",), {"default": None, "help": "pre-minted run id"}),
            (("--candidates",), {"default": "", "help": "comma-separated candidate names"}),
            (
                ("--include-live",),
                {"action": "store_true", "help": "also run the live effective contract as an arm"},
            ),
            (
                ("--eval-set",),
                {"default": None, "help": "named test set (default: the active one)"},
            ),
            (
                ("--arms-spec",),
                {
                    "default": None,
                    "help": "explicit arm list as JSON ([{source, name?, label, backend?}, ...])",
                },
            ),
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
