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
import time
import uuid
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

# Single-flight lease: one shared CAS-guarded file names the live run and the
# links it has executed. Guards the three dispatch paths that bypass
# process_manager's busy check (Cloud Tasks redelivery of a live task, the
# consolidate pipeline's raw dispatch, and self-chaining) — 2026-08-14 two
# concurrent runs embedded the same backlog slice and wrote twin shards.
_LEASE_FILE = "embeddings_run_lease.json"
_LEASE_LOCATION = "cache"
# A crashed run's lease stops blocking after this long — matches the Cloud
# Tasks task timeout, past which the original execution cannot still be alive.
_LEASE_STALE_S = 3600




def _claim_link(run_id: str, chunk: int) -> bool:
    """Atomically claim the right to execute link ``chunk`` of ``run_id``.

    Link 0 claims the whole run: it loses when a fresh lease names another
    run (a live chain — however it was dispatched). Later links lose when the
    lease moved to another run or their chunk was already executed (a Cloud
    Tasks redelivery of a link that outlived its dispatch deadline while the
    original execution kept running — the duplicate-shard failure mode).

    Args:
        run_id: This execution's run id.
        chunk: The link index it wants to execute.

    Returns:
        True when this execution won the claim.
    """
    import fyp.data_io as data_io

    claimed = {"won": False}

    def _mutate(lease):
        lease = lease if isinstance(lease, dict) else {}
        fresh = time.time() - float(lease.get("updated_at") or 0) <= _LEASE_STALE_S
        if chunk == 0:
            if fresh and lease.get("run_id") not in (None, run_id):
                return lease
            lease = {"run_id": run_id, "executed": {}}
        elif lease.get("run_id") != run_id:
            return lease
        executed = lease.setdefault("executed", {})
        if str(chunk) in executed:
            return lease
        executed[str(chunk)] = True
        lease["updated_at"] = time.time()
        claimed["won"] = True
        return lease

    data_io.update_json(storage_location=_LEASE_LOCATION, filename=_LEASE_FILE,
                        mutate=_mutate, default=None)
    return claimed["won"]




def _release_lease(run_id: str) -> None:
    """Blank the lease if this run still owns it (best-effort).

    A run that dies without releasing goes stale after ``_LEASE_STALE_S``
    and stops blocking on its own.
    """
    import fyp.data_io as data_io

    def _mutate(lease):
        if isinstance(lease, dict) and lease.get("run_id") == run_id:
            return {}
        return lease

    try:
        data_io.update_json(storage_location=_LEASE_LOCATION,
                            filename=_LEASE_FILE, mutate=_mutate, default=None)
    except Exception:
        pass


def run_embeddings_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Embed one batch of pending videos and optionally chain to the next.

    Args:
        reporter: Status reporter (GCS or local).
        task_args: Optional ``batch_size``, ``max_batches``, ``chunk_index``,
            ``initial_total``. Chain-internal: ``run_id`` (the single-flight
            lease owner; minted at link 0 when absent).

    Returns:
        Dict with ``chain=True`` and ``next_task_args`` if more batches remain,
        else ``None``.
    """
    from fyp.embeddings import active_embedding_backend, embed_pending

    task_args = task_args or {}
    batch_size = int(task_args.get("batch_size") or DEFAULT_BATCH_SIZE)
    max_batches = task_args.get("max_batches")
    if max_batches is not None:
        max_batches = int(max_batches)
    chunk_index = int(task_args.get("chunk_index", 0))
    initial_total = int(task_args.get("initial_total", 0))
    run_id = str(task_args.get("run_id") or uuid.uuid4().hex)

    if not _claim_link(run_id, chunk_index):
        reporter.log(
            "Another embeddings refresh is already live (or this link already "
            "ran) — skipping this dispatch to avoid duplicate shards."
        )
        reporter.update_progress(100, "Skipped — refresh already running")
        return None

    backend = active_embedding_backend()
    reporter.log(
        f"Embeddings refresh batch {chunk_index + 1} "
        f"(backend={backend.name}, {backend.model_id()}@{backend.dim()})..."
    )

    result = embed_pending(batch_size=batch_size, reporter=reporter)
    embedded = result["embedded"]
    remaining = result["remaining"]
    total = result["total"]

    # Keep the dense random-access sidecar (and the fingerprint-stamped corpus
    # mean) current — O(new shards), so this is one part per batch. A derived
    # cache must never fail the embedding run itself.
    try:
        from fyp.analysis import embedding_store

        embedding_store.ensure_dense_store(backend.model_id(), reporter=reporter)
    except Exception as exc:
        reporter.log(f"Dense-store compaction failed (non-fatal): {exc}")

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
        _release_lease(run_id)
        return None

    if embedded == 0:
        # Nothing was written (e.g. the whole batch failed); stop rather than
        # spin a chain that re-attempts the same failing head slice forever.
        reporter.log("No new embeddings written this batch; stopping chain.")
        _release_lease(run_id)
        return None

    if reporter.check_cancelled():
        reporter.log("Cancellation requested. Stopping after this batch.")
        _release_lease(run_id)
        return None

    next_chunk = chunk_index + 1
    if max_batches is not None and next_chunk >= max_batches:
        reporter.log(f"Reached max_batches limit ({max_batches}).")
        _release_lease(run_id)
        return None

    next_task_args = {
        "batch_size": batch_size,
        "max_batches": max_batches,
        "chunk_index": next_chunk,
        "initial_total": initial_total,
        "run_id": run_id,
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
