import sys
import time
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_aio_fetch(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Fetch recent AIO donations + participant metadata from AWS.

    Wraps the boto3-backed helpers in fyp.donations so the heavy network
    work runs on the task-runner service rather than blocking a data-hub
    request. AWS credentials are read from environment variables (loaded
    from Secret Manager on Cloud Run, ~/.aws/credentials locally).
    """
    from fyp.donations import (
        get_donation_metadata_from_aio_aws,
        get_recent_data_donations_from_aio_aws,
    )

    task_args = task_args or {}
    hours_back = int(task_args.get("hours_back", 24))

    _t_start = time.perf_counter()

    reporter.update_progress(
        0, f"Fetching donations from last {hours_back} hour(s)..."
    )
    fetch_result = get_recent_data_donations_from_aio_aws(
        hours_back=hours_back,
        storage_location="aio_raw",
    ) or {}
    donation_count = len(fetch_result.get("donation_ids", []))
    uploaded_count = fetch_result.get("uploaded_count", 0)
    _t_donations = time.perf_counter() - _t_start

    reporter.update_progress(
        70,
        f"Fetched {donation_count} donations ({uploaded_count} uploaded). "
        f"Refreshing participant metadata...",
    )
    _t_phase = time.perf_counter()
    get_donation_metadata_from_aio_aws(verbose=False)
    _t_metadata = time.perf_counter() - _t_phase

    _t_total = time.perf_counter() - _t_start
    reporter.emit_data({
        "hours_back": hours_back,
        "donations_found": donation_count,
        "donations_uploaded": uploaded_count,
    })
    reporter.update_progress(
        100,
        f"AIO fetch complete: {uploaded_count} donations uploaded "
        f"({_t_total:.0f}s).",
    )
    reporter.log(
        f"[TIMING] aio_fetch donations={_t_donations:.1f}s "
        f"metadata={_t_metadata:.1f}s total={_t_total:.1f}s "
        f"hours_back={hours_back} found={donation_count} uploaded={uploaded_count}"
    )

    return None




if __name__ == "__main__":
    import argparse

    from web_interface.task_status import LocalStatusReporter

    parser = argparse.ArgumentParser(description="Fetch AIO donations from AWS")
    parser.add_argument('--hours-back', type=int, default=24,
                        help='How many hours back to fetch donations from.')
    args = parser.parse_args()

    reporter = LocalStatusReporter("aio_fetch")
    try:
        run_aio_fetch(reporter=reporter, task_args={"hours_back": args.hours_back})
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
