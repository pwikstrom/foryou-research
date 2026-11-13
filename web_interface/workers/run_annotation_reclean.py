"""Re-clean stored annotation answers (Cloud Task / local subprocess).

Refines raw annotation files again with today's cleaning code and compares
the result with the stored refined rows (``fyp.annotation.annotation_reclean``).
The default is a DRY RUN, which writes nothing but its report; a real run
replaces the values of the variables it is given in the stored refined files
(keeping a copy of each original) and leaves a marker for the next
consolidation, which rebuilds the annotation dataset and reports the changed
videos. The worker never consolidates by itself.

Self-chaining: refining is slow (the old raw files need the lenient JSON
reader, roughly 20 answers a second), so the whole legacy corpus takes hours.
A link works for ``LINK_BUDGET_SECONDS`` and hands the rest to the next link;
the run's state is saved after every file, so a link that dies loses at most
the file it was on, and a file re-cleaned twice changes nothing the second
time. One file is in memory at a time.
"""

import time

from web_interface.tasks import worker_registry
from web_interface.tasks.task_status import TaskStatusReporter

# How long one link keeps starting new files. The file in flight when the
# budget runs out still finishes, and the largest legacy raw files (60 to
# 80 MB, about 10,000 answers) took 7 to 13 minutes each on a busy laptop
# core. A short budget keeps budget plus one such file well under the 1800 s
# dispatch deadline even on a slower machine.
LINK_BUDGET_SECONDS = 300


def run_annotation_reclean(reporter: TaskStatusReporter, task_args: dict | None = None):
    """Run a re-clean, or one link of it.

    task_args:
        dry_run: compare and report only (default True). False replaces the
            named variables in the stored refined files.
        variables: the variables to replace (list or comma-separated text).
            Required for a real run.
        only_legacy: restrict to files holding legacy-version annotations
            (default True).
        started_by: audit actor.
        chunk_index: the link's position in the chain (0 starts a new run;
            later links continue the stored state).

    Returns:
        ``None`` when the run is finished or cancelled; a chain instruction
        while files remain.
    """
    from fyp.annotation import annotation_reclean as reclean

    task_args = task_args or {}
    started = time.perf_counter()
    link = int(task_args.get("chunk_index") or 0)

    if link > 0:
        state = reclean.load_state()
        if not state or state.get("status") != "running":
            raise ValueError("no re-clean in progress to continue")
    else:
        reporter.update_progress(0, "Listing the annotation files...")
        variables = task_args.get("variables") or []
        if isinstance(variables, str):
            variables = variables.split(",")
        state = reclean.start_run(
            dry_run=task_args.get("dry_run") is not False,
            only_legacy=task_args.get("only_legacy") is not False,
            started_by=str(task_args.get("started_by") or ""),
            variables=list(variables),
        )
        reporter.log(
            "Variables to replace: "
            + (", ".join(state["variables"] or []) or "none named (every variable is compared)")
        )
        reporter.log(
            f"{'DRY RUN' if state['dry_run'] else 'REAL RUN'}: {len(state['files'])} raw file(s) "
            f"to refine again; {len(state['files_without_raw'])} stored file(s) have no raw "
            "answers and cannot be re-cleaned."
        )
    mode = "Dry run" if state["dry_run"] else "Re-clean"
    total = max(1, len(state["files"]))
    reporter.update_progress(
        min(int(state["next_index"] / total * 100), 99), f"{mode}: file {state['next_index']}"
    )

    def _progress(done: int, n_files: int, filename: str) -> None:
        reporter.update_progress(
            min(int(done / total * 100), 99), f"{mode}: {done}/{n_files} files"
        )

    def _should_pause() -> bool:
        return reporter.check_cancelled() or time.perf_counter() - started > LINK_BUDGET_SECONDS

    state = reclean.continue_run(state, should_pause=_should_pause, progress_cb=_progress)

    if state["status"] == "running":
        if reporter.check_cancelled():
            state = reclean.finish_run(state, status="cancelled")
            reporter.log(
                f"Cancelled after {state['next_index']} of {len(state['files'])} files. "
                + _outcome(state)
            )
            return None
        reporter.log(
            f"[TIMING] annotation_reclean link={link} "
            f"total={time.perf_counter() - started:.1f}s files_done={state['next_index']}"
        )
        return {
            "chain": True,
            "next_task_args": {
                "chunk_index": link + 1,
                "started_by": state.get("started_by", ""),
            },
            "dispatch_deadline_seconds": worker_registry.deadline_for("annotation_reclean"),
            "chain_log_message": (
                f"{state['next_index']} of {len(state['files'])} files done; "
                "continuing in a new task."
            ),
        }

    report = state["report"]
    reporter.emit_data(
        {
            "dry_run": state["dry_run"],
            "files": report["files"],
            "rows_changed": report["rows_changed"],
            "files_blocked": len(report["files_blocked"]),
        }
    )
    top = sorted(report["values_changed"].items(), key=lambda kv: -kv[1])[:8]
    reporter.log(
        "Values that differ, by variable: " + (", ".join(f"{k} {v:,}" for k, v in top) or "none")
    )
    reporter.log(f"Done. {_outcome(state)}")
    return None


def _outcome(state: dict) -> str:
    """One sentence on what the run did and what to do next."""
    report = state["report"]
    counts = (
        f"{report['rows_changed']:,} of {report['rows']:,} stored rows differ in "
        f"{report['files_changed']} file(s); {len(report['files_blocked'])} file(s) left alone; "
        f"{report['rows_cleaning_fails_on']:,} row(s) today's cleaning cannot read keep "
        "their stored values."
    )
    if state["dry_run"]:
        return counts + " Nothing was written; the full report is in the re-clean report."
    if not state["changed_item_ids"]:
        return counts + " Nothing needed replacing."
    return (
        counts + f" {', '.join(state['variables'])} replaced in the stored files (originals "
        "kept). Run Consolidate (Data Pipeline -> Dataset "
        "Assembly) to rebuild the annotation dataset and refresh the studies."
    )


if __name__ == "__main__":
    import functools

    from web_interface.tasks.worker_runner import chain_locally, run_worker

    run_worker(
        functools.partial(chain_locally, run_annotation_reclean),
        "annotation_reclean",
        arg_specs=[
            (
                ("--apply",),
                {
                    "action": "store_true",
                    "help": "replace the stored refined files (default: dry run, writes nothing)",
                },
            ),
            (
                ("--all-files",),
                {
                    "action": "store_true",
                    "help": "include files without legacy-version annotations",
                },
            ),
            (
                ("--variables",),
                {
                    "default": "",
                    "help": "comma-separated variables to replace (required with --apply)",
                },
            ),
            (("--started-by",), {"default": None, "help": "audit actor"}),
        ],
        make_task_args=lambda cli: {
            "dry_run": not cli.apply,
            "only_legacy": not cli.all_files,
            "variables": cli.variables,
            "started_by": cli.started_by,
        },
        description="Re-clean stored annotation answers with today's cleaning code",
    )
