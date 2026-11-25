"""Backlog annotator: a machine's own annotator works through the annotation backlog.

Runs on a machine that shares the Hub's storage but is not the hosted Hub (a
Mac with a local model). It claims a chunk of the backlog, annotates it with
this machine's annotator, writes the raw and refined result files to the
shared storage, and repeats. It never consolidates: the hosted Hub does, and
picks the new files up on its next consolidation.

    FYP_FORCE_GCS=1 FYP_ANNOTATION_BACKEND=qwen_local \\
        python -m web_interface.workers.backlog_annotator --chunk 50

``FYP_ANNOTATION_BACKEND`` is required: this worker is for a machine's own
annotator, and refuses to run on the shared, admin-selected one. Several
copies may run against one backlog (claims keep them apart). Ctrl-C or
SIGTERM stops after the chunk in progress; a killed worker loses only that
chunk, which returns to the backlog when its claim goes stale.

Not a registered Hub worker (hence no ``run_`` prefix): it is started by hand
or as a service on the second machine, never from the web UI or Cloud Tasks.

See ``web_interface/services/annotation_backlog.py`` for the files involved
and the DEVELOPING.md runbook for operating it.
"""

import functools
import os
import signal
import socket
import time

# A chunk that brings back no result at all (its results could not be read
# back) is retried later; this many in a row stop the worker.
MAX_EMPTY_CHUNKS = 3
# Seconds to wait before looking again when every id in reach is spoken for.
IDLE_WAIT_S = 120


def run(
    chunk: int = 50,
    max_chunks: int | None = None,
    stop_requested=None,
    annotate=None,
    sleep=time.sleep,
) -> dict:
    """Work through the backlog until it is empty, a limit is hit, or a stop is asked.

    Args:
        chunk: Videos per claim.
        max_chunks: Stop after this many annotated chunks (``None`` = no limit).
        stop_requested: ``() -> bool``; checked between chunks.
        annotate: ``(ids) -> (ok_ids, fail_ids)``; defaults to the Hub's
            ``annotate_from_video_id_list`` (injected in tests).
        sleep: Injected in tests.

    Returns:
        ``{"chunks", "ok", "fail", "dropped", "remaining", "reason"}``.
    """
    from fyp.annotation import annotation_versioning
    from fyp.annotation.backends import active_backend_name
    from web_interface.services import annotation_backlog as backlog

    if annotate is None:
        from fyp.annotation.machine_annotation import annotate_from_video_id_list

        def annotate(ids):
            return annotate_from_video_id_list(fine_list=ids, verbose=False, dry_run=False)

    stop_requested = stop_requested or (lambda: False)
    backend = active_backend_name()
    started = backlog._now()
    totals = {"chunks": 0, "ok": 0, "fail": 0, "dropped": 0}
    empty_in_a_row = 0
    reason = ""

    def _status(state: str, **extra) -> None:
        elapsed = max((backlog._now() - started).total_seconds(), 1.0)
        done = totals["ok"] + totals["fail"]
        backlog.write_status(
            state=state,
            backend=backend,
            annotation_version=annotation_versioning.active_annotation_version(),
            started_at=started.isoformat(),
            chunk_size=chunk,
            seconds_per_video=round(elapsed / done, 2) if done else None,
            **totals,
            **extra,
        )

    remaining = len(backlog.load_backlog()["items"])
    _status("running", remaining=remaining)
    print(f"Backlog annotator on {backend}: {remaining:,} video(s) listed.", flush=True)

    while True:
        if stop_requested():
            reason = "stopped by request"
            break
        if max_chunks is not None and totals["chunks"] >= max_chunks:
            reason = f"reached the {max_chunks}-chunk limit"
            break
        claimed = backlog.claim(chunk)
        if claimed is None:
            if backlog.load_backlog()["items"]:
                # Ids remain, but all in reach are claimed elsewhere or with the
                # hosted annotator. Wait for them to clear.
                _status(
                    "running", remaining=remaining, note="waiting: remaining ids are spoken for"
                )
                sleep(IDLE_WAIT_S)
                continue
            reason = "backlog empty"
            break
        totals["dropped"] += len(claimed["dropped"])
        if not claimed["items"]:
            continue  # this pass only cleared ids that no longer need annotating

        claim_id, ids = claimed["claim_id"], claimed["items"]
        try:
            beat = functools.partial(_status, "running", remaining=remaining, in_progress=len(ids))
            with backlog.ClaimHeartbeat(claim_id, on_beat=beat):
                ok_ids, fail_ids = annotate(ids)
        except BaseException:
            backlog.release(claim_id)
            _status("stopped", remaining=remaining, note="stopped on an error")
            raise

        if not ok_ids and not fail_ids:
            backlog.release(claim_id)
            empty_in_a_row += 1
            print(
                f"Chunk of {len(ids)} returned nothing readable; left in the backlog.", flush=True
            )
            if empty_in_a_row >= MAX_EMPTY_CHUNKS:
                reason = f"stopped: {MAX_EMPTY_CHUNKS} chunks in a row returned nothing"
                break
            continue
        empty_in_a_row = 0
        remaining = backlog.complete(claim_id, ok_ids, fail_ids)
        totals["chunks"] += 1
        totals["ok"] += len(ok_ids)
        totals["fail"] += len(fail_ids)
        _status("running", remaining=remaining)
        print(
            f"Chunk {totals['chunks']}: {len(ok_ids)} annotated, {len(fail_ids)} failed; "
            f"{remaining:,} left.",
            flush=True,
        )

    _status("stopped", remaining=remaining, note=reason)
    print(f"Backlog annotator finished: {reason}.", flush=True)
    return {**totals, "remaining": remaining, "reason": reason}


def main() -> int:
    import argparse

    from fyp.annotation import gemini_calls
    from fyp.annotation.backends import MACHINE_BACKEND_ENV, machine_backend_override

    parser = argparse.ArgumentParser(
        description="Annotate the backlog with this machine's annotator"
    )
    parser.add_argument("--chunk", type=int, default=50, help="Videos per claim (default 50)")
    parser.add_argument("--max-chunks", type=int, default=None, help="Stop after this many chunks")
    parser.add_argument(
        "--list-eligible",
        action="store_true",
        help="Write the backlog from every video that may be annotated, then exit",
    )
    args = parser.parse_args()

    if args.list_eligible:
        from web_interface.services import annotation_backlog as backlog

        n = backlog.set_backlog(backlog.eligible_ids())
        print(f"Backlog written: {n:,} video(s).")
        return 0

    if machine_backend_override() is None:
        print(
            f"{MACHINE_BACKEND_ENV} is not set (or names no known annotator). This worker runs "
            f"a machine's own annotator, never the shared one. Example: "
            f"{MACHINE_BACKEND_ENV}=qwen_local"
        )
        return 2
    # Tag this machine's result files unless the operator chose a tag.
    os.environ.setdefault(gemini_calls.MACHINE_TAG_ENV, socket.gethostname().split(".")[0])

    stop = {"asked": False}

    def _ask_stop(signum, frame):
        if stop["asked"]:
            raise KeyboardInterrupt
        stop["asked"] = True
        print("Stop requested: finishing the chunk in progress (again to stop now).", flush=True)

    signal.signal(signal.SIGINT, _ask_stop)
    signal.signal(signal.SIGTERM, _ask_stop)

    run(chunk=args.chunk, max_chunks=args.max_chunks, stop_requested=lambda: stop["asked"])
    return 0


if __name__ == "__main__":
    code = main()
    # A stuck worker thread must not keep the process alive (as in run_queue_annotator).
    os._exit(code)
