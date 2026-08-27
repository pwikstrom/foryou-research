import sys
import time
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_ops_report(reporter: TaskStatusReporter, task_args: dict | None = None) -> dict | None:
    """Generate the daily admin ops report.

    Collects every status check (users, workers, queues, cookies, alerts,
    Cloud Run logs, dependencies), asks Gemini for the written assessment,
    renders the colour-coded HTML board, stores it under cache/ops_report/,
    and emails it to the configured admin address. Read-only against the
    datasets. Deliberately NOT queue-retry-safe: a retry would re-send the
    email, so a failed run goes straight to the task-failures ledger.
    """
    from web_interface.services.ops_report import generate_ops_report

    task_args = task_args or {}
    hours_back = int(task_args.get("hours_back", 24))
    send_email = str(task_args.get("send_email", "true")).lower() not in (
        "false", "0", "no")

    _t_start = time.perf_counter()
    result = generate_ops_report(reporter=reporter, hours_back=hours_back,
                                 send_email=send_email)
    reporter.emit_data({
        "hours_back": hours_back,
        "overall": result.get("overall"),
        "red": result.get("counts", {}).get("red", 0),
        "yellow": result.get("counts", {}).get("yellow", 0),
        "narrative_source": result.get("narrative_source"),
        "email_sent": result.get("email_sent"),
    })
    reporter.log(f"[TIMING] ops_report total={time.perf_counter() - _t_start:.1f}s "
                 f"overall={result.get('overall')} "
                 f"email_sent={result.get('email_sent')}")
    return None


if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    run_worker(
        run_ops_report,
        "ops_report",
        arg_specs=[
            (('--hours-back',), {'type': int, 'default': 24,
                                 'help': 'Reporting window in hours.'}),
            (('--no-email',), {'action': 'store_true',
                               'help': 'Generate and store without emailing.'}),
        ],
        make_task_args=lambda args: {"hours_back": args.hours_back,
                                     "send_email": not args.no_email},
        description="Daily admin ops status report",
    )
