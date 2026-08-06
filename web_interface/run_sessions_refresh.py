import sys
import time
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_sessions_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Rebuild the Sessions-tab artifacts (session index + focus episodes).

    Loads the active embedding model's directional store, segments every
    collection's viewing sessions into focus episodes, and persists
    ``sessions_index.parquet`` / ``session_episodes.parquet`` /
    ``sessions_meta.json`` in the ``cache`` location (see
    :mod:`fyp.analysis.session_explorer`).

    Args:
        reporter: Status reporter (GCS on Cloud Run, stdout locally).
        task_args: Optional dict. Recognised keys: ``collections``
            (comma-separated collection ids to restrict to), plus the
            segmentation overrides ``cut``, ``mem``, ``min_videos``,
            ``min_minutes``, ``window_n``.
    """
    from fyp.analysis import session_explorer

    task_args = task_args or {}
    reporter.log("Starting Sessions refresh...")
    _t_run_start = time.perf_counter()

    params: dict = {}
    for key, cast in (("cut", float), ("mem", int), ("min_videos", int),
                      ("min_minutes", float), ("window_n", int), ("max_windows", int)):
        if task_args.get(key) is not None:
            params[key] = cast(task_args[key])

    collections = None
    collections_str = task_args.get("collections")
    if collections_str:
        collections = [c.strip() for c in str(collections_str).split(",") if c.strip()]
        reporter.log(f"Targeted refresh for {len(collections)} collection(s).")

    meta = session_explorer.build_artifacts(
        reporter=reporter, params=params or None, collections=collections,
    )
    if meta.get("cancelled"):
        return

    reporter.update_progress(100, "Done")
    _t_run = time.perf_counter() - _t_run_start
    reporter.log(
        f"[TIMING] sessions_refresh wall={_t_run:.2f}s "
        f"collections={meta.get('n_collections')} sessions={meta.get('n_sessions')} "
        f"episodes={meta.get('n_episodes')} model={meta.get('embedding_model')}"
    )
    reporter.log("Sessions refresh completed.")




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    def _make_task_args(args) -> dict:
        task_args: dict = {}
        if args.collections:
            task_args["collections"] = args.collections
        for key in ("cut", "mem", "min_videos", "min_minutes", "window_n", "max_windows"):
            value = getattr(args, key, None)
            if value is not None:
                task_args[key] = value
        return task_args

    run_worker(
        run_sessions_refresh,
        "sessions_refresh",
        arg_specs=[
            (("--collections",), {"type": str, "default": None,
                                  "help": "Comma-separated collection ids to refresh (default: all)"}),
            (("--cut",), {"type": float, "default": None}),
            (("--mem",), {"type": int, "default": None}),
            (("--min-videos",), {"type": int, "default": None}),
            (("--min-minutes",), {"type": float, "default": None}),
            (("--window-n",), {"type": int, "default": None}),
            (("--max-windows",), {"type": int, "default": None}),
        ],
        make_task_args=_make_task_args,
    )
