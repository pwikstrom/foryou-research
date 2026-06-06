"""Embeddings refresh: embed not-yet-embedded annotated videos.

On Cloud Run this runs as a single-batch Cloud Task that self-chains to the
next batch until the embedding backlog is exhausted, the user's max_batches
limit is reached, or the user cancels. Each batch writes one new shard to the
``recoded`` store (see :mod:`fyp.embeddings`).

Locally it loops over batches in a single subprocess until the backlog is
empty.
"""

import os
import sys
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter

# Items embedded per Cloud Task invocation. ~112 videos/s with 8 workers, so
# 20k ≈ 3 min of embedding plus shard I/O — comfortably inside the 3600s
# Cloud Tasks timeout.
DEFAULT_BATCH_SIZE = 20000
_DISPATCH_DEADLINE = 1800


def run_embeddings_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Embed one batch of pending videos and optionally chain to the next.

    Args:
        reporter: Status reporter (GCS or local).
        task_args: Optional ``batch_size``, ``max_batches``, ``chunk_index``,
            ``initial_total``.

    Returns:
        Dict with ``chain=True`` and ``next_task_args`` if more batches remain,
        else ``None``.
    """
    from fyp.embeddings import EMBED_DIM, EMBED_MODEL, embed_pending

    task_args = task_args or {}
    batch_size = int(task_args.get("batch_size") or DEFAULT_BATCH_SIZE)
    max_batches = task_args.get("max_batches")
    if max_batches is not None:
        max_batches = int(max_batches)
    chunk_index = int(task_args.get("chunk_index", 0))
    initial_total = int(task_args.get("initial_total", 0))

    reporter.log(f"Embeddings refresh batch {chunk_index + 1} ({EMBED_MODEL}@{EMBED_DIM})...")

    result = embed_pending(batch_size=batch_size, reporter=reporter)
    embedded = result["embedded"]
    remaining = result["remaining"]
    total = result["total"]

    # Captured on the first chain so progress framing stays stable as the
    # backlog shrinks across chains.
    already = total - remaining - embedded
    if initial_total <= 0:
        initial_total = total
    done = max(0, total - remaining)
    pct = int(done / total * 100) if total else 100
    reporter.update_progress(pct, f"Embedded {done:,}/{total:,} videos")
    reporter.emit_data({"embeddings_total": done, "embeddings_remaining": remaining})
    reporter.log(
        f"Batch {chunk_index + 1} complete: +{embedded:,} embedded, "
        f"{remaining:,} remaining of {total:,}."
    )

    if remaining <= 0:
        reporter.log("Embedding backlog exhausted.")
        return None

    if embedded == 0:
        # Nothing was written (e.g. the whole batch failed); stop rather than
        # spin a chain that re-attempts the same failing head slice forever.
        reporter.log("No new embeddings written this batch; stopping chain.")
        return None

    if reporter.check_cancelled():
        reporter.log("Cancellation requested. Stopping after this batch.")
        return None

    next_chunk = chunk_index + 1
    if max_batches is not None and next_chunk >= max_batches:
        reporter.log(f"Reached max_batches limit ({max_batches}).")
        return None

    next_task_args = {
        "batch_size": batch_size,
        "max_batches": max_batches,
        "chunk_index": next_chunk,
        "initial_total": initial_total,
    }
    reporter.log(f"Chaining to next batch (chunk_index={next_chunk})...")
    return {
        "chain": True,
        "next_task_args": next_task_args,
        "dispatch_deadline_seconds": _DISPATCH_DEADLINE,
    }




if __name__ == "__main__":
    import argparse

    from web_interface.task_status import LocalStatusReporter

    parser = argparse.ArgumentParser(description="Embed pending annotated videos")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help="Videos embedded per batch")
    parser.add_argument("--max-batches", type=int, default=None,
                        help="Max batches to run (default: until backlog empty)")
    args = parser.parse_args()

    reporter = LocalStatusReporter("embeddings_refresh")
    task_args = {"batch_size": args.batch_size, "max_batches": args.max_batches}
    batches_run = 0
    try:
        while True:
            task_args["chunk_index"] = batches_run
            chain = run_embeddings_refresh(reporter=reporter, task_args=task_args)
            batches_run += 1
            if not chain:
                break
            task_args = chain["next_task_args"]
        reporter.complete()
        print("Embeddings refresh completed.")
        os._exit(0)
    except Exception as e:
        reporter.fail(str(e))
        print(f"Embeddings refresh failed: {e}")
        import traceback
        traceback.print_exc()
        os._exit(1)
