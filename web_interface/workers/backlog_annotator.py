"""Backlog annotator: a machine's own annotator works through the annotation backlog.

Runs on a machine that shares the Hub's storage but is not the hosted Hub (a
Mac with a local model). It claims a chunk of the backlog, annotates it with
this machine's annotator, writes the raw and refined result files to the
shared storage, and repeats. It never consolidates: the hosted Hub does, and
picks the new files up on its next consolidation.

    FYP_FORCE_GCS=1 FYP_ANNOTATION_BACKEND=qwen_local \\
        python -m web_interface.workers.backlog_annotator run

``FYP_ANNOTATION_BACKEND`` is required for ``run``: this tool is for a
machine's own annotator, and refuses to run on the shared, admin-selected one.
Several copies may run against one backlog (claims keep them apart). Ctrl-C or
SIGTERM stops after the chunk in progress; a killed process loses only that
chunk, which returns to the backlog when its claim goes stale. With an empty
or paused backlog ``run`` waits and looks again.

The other commands inspect and steer the backlog from the same machine::

    ... backlog_annotator status
    ... backlog_annotator add --study "My study" [--lane urgent] [--front]
    ... backlog_annotator add --collection <id> | --file ids.txt | --all
    ... backlog_annotator remove --file ids.txt
    ... backlog_annotator empty [--lane urgent]
    ... backlog_annotator pause | resume | retry-failed

Not a registered Hub worker (hence no ``run_`` prefix): it is started by hand
or as a service on the second machine, never from the web UI or Cloud Tasks.

See ``web_interface/services/annotation_backlog.py`` for the files involved
and the DEVELOPING.md runbook for operating it.
"""

import functools
import os
import signal
import socket
import subprocess
import sys
import time

# A chunk that brings back no result at all (its results could not be read
# back) is retried later; this many in a row stop the run.
MAX_EMPTY_CHUNKS = 3
# Seconds to wait before looking again when there is nothing to take: every id
# in reach is spoken for, the backlog is paused, or it is empty.
IDLE_WAIT_S = 120
DEFAULT_CHUNK = 50


def run(
    chunk: int = DEFAULT_CHUNK,
    max_chunks: int | None = None,
    stop_requested=None,
    annotate=None,
    sleep=time.sleep,
    wait_when_empty: bool = False,
    log=None,
) -> dict:
    """Work through the backlog until a limit is hit or a stop is asked.

    Args:
        chunk: Videos per claim.
        max_chunks: Stop after this many annotated chunks (``None`` = no limit).
        stop_requested: ``() -> bool``; checked between chunks and while waiting.
        annotate: ``(ids) -> (ok_ids, fail_ids)``; defaults to the Hub's
            ``annotate_from_video_id_list`` (injected in tests).
        sleep: Injected in tests.
        wait_when_empty: Keep waiting for new ids when the backlog is empty
            (a service); otherwise an empty backlog ends the run.
        log: ``(line) -> None`` for the run's own lines (default: print).

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
    say = log or functools.partial(print)
    backend = active_backend_name()
    started = backlog._now()
    totals = {"chunks": 0, "ok": 0, "fail": 0, "dropped": 0}
    empty_in_a_row = 0
    reason = ""
    last_chunk_at = None  # when this run last wrote result files

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
            last_chunk_at=last_chunk_at,
            **totals,
            **extra,
        )

    remaining = len(backlog.load_backlog()["items"])
    _status("running", remaining=remaining)
    say(f"Backlog annotator on {backend}: {remaining:,} video(s) listed.")

    while True:
        if stop_requested():
            reason = "stopped by request"
            break
        if max_chunks is not None and totals["chunks"] >= max_chunks:
            reason = f"reached the {max_chunks}-chunk limit"
            break
        claimed = backlog.claim(chunk)
        if claimed is None:
            state = backlog.load_backlog()
            remaining = len(state["items"])
            if state["paused"]:
                note = "paused"
            elif state["items"]:
                # All ids in reach are claimed elsewhere or with the hosted annotator.
                note = "waiting: remaining videos are spoken for"
            elif wait_when_empty:
                note = "waiting: the backlog is empty"
            else:
                reason = "backlog empty"
                break
            _status("running", remaining=remaining, note=note)
            sleep(IDLE_WAIT_S)
            continue
        totals["dropped"] += len(claimed["dropped"])
        if not claimed["items"]:
            continue  # this pass only cleared ids that no longer need annotating

        claim_id, ids = claimed["claim_id"], claimed["items"]
        try:
            beat = functools.partial(_status, "running", remaining=remaining, in_progress=len(ids))
            with backlog.ClaimHeartbeat(claim_id, on_beat=beat):
                ok_ids, fail_ids = annotate(ids)
        except BaseException as exc:
            backlog.release(claim_id)
            _status("stopped", remaining=remaining, note=f"stopped on an error: {exc!r}"[:300])
            raise

        if not ok_ids and not fail_ids:
            backlog.release(claim_id)
            empty_in_a_row += 1
            say(f"Chunk of {len(ids)} returned nothing readable; left in the backlog.")
            if empty_in_a_row >= MAX_EMPTY_CHUNKS:
                reason = f"stopped: {MAX_EMPTY_CHUNKS} chunks in a row returned nothing"
                break
            continue
        empty_in_a_row = 0
        remaining = backlog.complete(claim_id, ok_ids, fail_ids)
        last_chunk_at = backlog._now().isoformat()
        totals["chunks"] += 1
        totals["ok"] += len(ok_ids)
        totals["fail"] += len(fail_ids)
        _status("running", remaining=remaining)
        say(
            f"{backlog._now().astimezone():%Y-%m-%d %H:%M} chunk {totals['chunks']}: "
            f"{len(ok_ids)} annotated, {len(fail_ids)} failed; {remaining:,} left."
        )

    _status("stopped", remaining=remaining, note=reason)
    say(f"Backlog annotator finished: {reason}.")
    return {**totals, "remaining": remaining, "reason": reason}


def daily_log(folder: str):
    """A ``(line) -> None`` that prints and appends to ``<folder>/backlog_annotator_<date>.log``."""
    os.makedirs(folder, exist_ok=True)

    def _log(line: str) -> None:
        print(line, flush=True)
        name = f"backlog_annotator_{time.strftime('%Y-%m-%d')}.log"
        with open(os.path.join(folder, name), "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {line}\n")

    return _log


def notify(message: str) -> None:
    """A desktop notification on macOS (best effort; silent elsewhere)."""
    if sys.platform != "darwin":
        return
    try:
        script = f'display notification "{message[:200]}" with title "For You Data Hub"'
        subprocess.run(["osascript", "-e", script.replace("\\", "")], timeout=10, check=False)
    except Exception:
        pass


def status_text() -> str:
    """The backlog and the worker's last report, as plain lines."""
    from web_interface.services import annotation_backlog as backlog

    s = backlog.read_status()
    worker = s["worker"] or {}
    lines = [
        f"Backlog: {s['remaining']:,} video(s) left"
        + (" (PAUSED)" if s["paused"] else "")
        + f"; {s['claimed']:,} being annotated now; {s['failed']:,} failed.",
        "Lanes:   " + ", ".join(f"{lane} {n:,}" for lane, n in s["lanes"].items()),
    ]
    if not worker:
        lines.append("Worker:  has never reported.")
        return "\n".join(lines)
    done = int(worker.get("ok") or 0) + int(worker.get("fail") or 0)
    lines.append(
        f"Worker:  {'RUNNING' if s['running'] else 'NOT RUNNING'} on {worker.get('host')} "
        f"({worker.get('backend')}, version {worker.get('annotation_version')})"
    )
    lines.append(
        f"         since {str(worker.get('started_at'))[:16]} UTC: {done:,} done "
        f"({worker.get('fail', 0):,} failed, {worker.get('dropped', 0):,} no longer needed)"
    )
    speed = worker.get("seconds_per_video")
    if speed:
        days = s["remaining"] * float(speed) / 86400
        lines.append(f"         {speed} s per video; about {days:.1f} day(s) for what is left")
    lines.append(f"         last heard {str(worker.get('heartbeat_at'))[:19]} UTC")
    if worker.get("note"):
        lines.append(f"         note: {worker['note']}")
    return "\n".join(lines)


def _read_ids(path: str) -> list[str]:
    with open(path, encoding="utf-8") as handle:
        return [line.split(",")[0].strip() for line in handle if line.strip()]


def main(argv=None) -> int:
    import argparse

    from fyp.annotation import gemini_calls
    from fyp.annotation.backends import MACHINE_BACKEND_ENV, machine_backend_override
    from web_interface.services import annotation_backlog as backlog

    parser = argparse.ArgumentParser(description="The annotation backlog on a second machine")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="Annotate the backlog with this machine's annotator")
    p_run.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="Videos per claim")
    p_run.add_argument("--max-chunks", type=int, default=None, help="Stop after this many chunks")
    p_run.add_argument(
        "--exit-when-empty", action="store_true", help="Stop when the backlog is empty"
    )
    p_run.add_argument(
        "--log-dir", default=None, help="Also write the run's lines to a dated file here"
    )
    sub.add_parser("status", help="Show the backlog and the worker's last report")
    p_add = sub.add_parser("add", help="Put videos in the backlog")
    source = p_add.add_mutually_exclusive_group(required=True)
    source.add_argument("--study", help="A study's videos that still need annotating")
    source.add_argument("--collection", help="A collection's videos that still need annotating")
    source.add_argument("--file", help="A text file with one video id per line")
    source.add_argument("--all", action="store_true", help="Every video that may be annotated")
    p_add.add_argument("--lane", choices=backlog.LANES, default=None)
    p_add.add_argument("--front", action="store_true", help="Put them at the front of the lane")
    p_remove = sub.add_parser("remove", help="Take videos out of the backlog")
    p_remove.add_argument("--file", required=True, help="A text file with one video id per line")
    p_empty = sub.add_parser("empty", help="Empty one lane, or the whole backlog")
    p_empty.add_argument("--lane", choices=backlog.LANES, default=None)
    sub.add_parser("pause", help="Workers stop taking new chunks")
    sub.add_parser("resume", help="Workers carry on")
    sub.add_parser("retry-failed", help="List the failed videos again")
    args = parser.parse_args(argv)

    if args.command == "status":
        print(status_text())
    elif args.command == "add":
        if args.all:
            out = backlog.add(backlog.eligible_ids(), lane=args.lane or "rest", front=args.front)
            print(f"Added {out['added']:,} video(s). Lanes: {out['lanes']}")
        else:
            out = backlog.add_from(
                study=args.study,
                collection=args.collection,
                item_ids=_read_ids(args.file) if args.file else None,
                lane=args.lane or "urgent",
                front=args.front,
            )
            print(
                f"{out['source']}: {out['candidates']:,} video(s), {out['eligible']:,} may be "
                f"annotated, {out['added']:,} added. Lanes: {out['lanes']}"
            )
    elif args.command == "remove":
        print(f"{backlog.remove(_read_ids(args.file)):,} video(s) left in the backlog.")
    elif args.command == "empty":
        backlog.empty(args.lane)
        print(f"Emptied {args.lane or 'every lane'}.")
    elif args.command in ("pause", "resume"):
        backlog.set_paused(args.command == "pause")
        print("Paused." if args.command == "pause" else "Resumed.")
    elif args.command == "retry-failed":
        print(f"{backlog.retry_failed():,} failed video(s) listed again.")
    else:
        if machine_backend_override() is None:
            print(
                f"{MACHINE_BACKEND_ENV} is not set (or names no known annotator). This tool runs "
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
            print(
                "Stop requested: finishing the chunk in progress (again to stop now).", flush=True
            )

        signal.signal(signal.SIGINT, _ask_stop)
        signal.signal(signal.SIGTERM, _ask_stop)
        try:
            out = run(
                chunk=args.chunk,
                max_chunks=args.max_chunks,
                stop_requested=lambda: stop["asked"],
                wait_when_empty=not args.exit_when_empty,
                log=daily_log(args.log_dir) if args.log_dir else None,
            )
        except BaseException as exc:
            notify(f"Backlog annotation stopped on an error: {exc!r}")
            raise
        if out["reason"] not in ("stopped by request", "backlog empty") and not (
            args.max_chunks and out["reason"].startswith("reached")
        ):
            notify(f"Backlog annotation stopped: {out['reason']}")
    return 0


if __name__ == "__main__":
    code = main()
    # A stuck worker thread must not keep the process alive (as in run_queue_annotator).
    os._exit(code)
