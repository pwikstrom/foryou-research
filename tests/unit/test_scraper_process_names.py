"""Pin the per-platform scraper process wiring.

One worker process per registered platform (queue_scraper_<platform>), no
bare queue_scraper process entry, Cloud Tasks eligibility, and --platform
CLI round-tripping through the subprocess/Cloud-Tasks arg converters.
"""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from web_interface.process_manager import (
    CLOUD_TASK_ELIGIBLE,
    SCRAPER_PROCESS_NAMES,
    _cli_args_to_dict,
    _task_args_to_cli,
    processes,
)


def main() -> int:
    assert SCRAPER_PROCESS_NAMES == ["queue_scraper_tiktok"], SCRAPER_PROCESS_NAMES

    for name in SCRAPER_PROCESS_NAMES:
        assert name in processes, f"{name} missing from process state registry"
        assert name in CLOUD_TASK_ELIGIBLE, f"{name} missing from CLOUD_TASK_ELIGIBLE"
    assert "queue_scraper" not in processes, "bare queue_scraper process entry must be gone"
    assert "queue_scraper" not in CLOUD_TASK_ELIGIBLE, "bare queue_scraper must not be dispatchable"

    # --platform round-trips through both arg converters
    args = ["--batch-size", "500", "--max-batches", "3", "--platform", "tiktok"]
    task_args = _cli_args_to_dict("queue_scraper_tiktok", args, None)
    assert task_args.get("platform") == "tiktok", task_args
    cli = _task_args_to_cli("queue_scraper_tiktok", task_args)
    assert cli == args, cli

    # The Cloud Tasks task-function registry must accept every platform name
    # plus the bare-name transition alias for in-flight chains.
    from web_interface.routes.process_routes import TASK_FUNCTIONS, _ensure_task_functions_loaded
    _ensure_task_functions_loaded()
    for name in SCRAPER_PROCESS_NAMES + ["queue_scraper"]:
        assert name in TASK_FUNCTIONS, f"{name} missing from TASK_FUNCTIONS"

    print("OK — per-platform scraper process wiring pinned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
