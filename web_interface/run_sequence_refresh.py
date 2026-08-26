import sys
import time
from pathlib import Path

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.append(str(project_root))

from web_interface.task_status import TaskStatusReporter


def run_sequence_refresh(reporter: TaskStatusReporter, task_args: dict | None = None) -> None:
    """Refresh sequence-analysis artifacts (dwell→next-window lift) for studies.

    For each study with annotated videos, builds the per-window table and the
    summary lift/transition grid, persisting them as ``{study}_sequence.parquet``
    and ``{study}_sequence_summary.json`` in the ``cache`` location.

    Args:
        reporter: Status reporter (GCS on Cloud Run, stdout locally).
        task_args: Optional dict. Recognised keys: ``studies`` (comma-separated
            study names to target), ``window_n``, ``session_gap_s``.
    """
    from fyp import data_io, sequence_analysis
    from fyp.fyp_config import fyp_cf
    from fyp.studies import init_study_defs

    task_args = task_args or {}
    reporter.log("Starting Sequence Analysis Refresh...")
    _t_run_start = time.perf_counter()

    window_n = int(task_args.get("window_n", sequence_analysis.DEFAULT_WINDOW_N))
    session_gap_s = int(task_args.get("session_gap_s", sequence_analysis.SESSION_GAP_S))

    init_study_defs()
    studies = fyp_cf.get("study_defs", {})

    target_studies_str = task_args.get("studies")
    if target_studies_str:
        target_names = [s.strip() for s in target_studies_str.split(",")]
        studies = {k: v for k, v in studies.items() if k in target_names}
        reporter.log(f"Targeted refresh for {len(studies)} study/studies: {', '.join(studies.keys())}")

    # System-managed participant studies refresh only when explicitly targeted
    # (their owner's collections changed, or a consolidation impact named
    # them) — a full sweep must stay O(regular studies), not O(participants).
    # Composed ("Everyone & Me") defs store no artifacts and never run here.
    from fyp.studies import is_composed_study, is_system_study
    _skipped_system = sorted(
        k for k, v in studies.items()
        if is_composed_study(v) or (is_system_study(v) and not target_studies_str)
    )
    if _skipped_system:
        studies = {k: v for k, v in studies.items() if k not in _skipped_system}
        reporter.log(f"Skipping {len(_skipped_system)} system-managed study/studies.")

    total = len(studies)
    if total == 0:
        reporter.log("No studies found to refresh.")
        return

    for i, (study_name, config) in enumerate(studies.items()):
        if reporter.check_cancelled():
            reporter.log("Cancelled by user.")
            break
        reporter.update_progress(int((i / total) * 100), f"Processing {study_name} ({i + 1}/{total})...")
        reporter.log(f"Processing study: {study_name}")
        _t_study_start = time.perf_counter()

        try:
            stats = config.get("stats", {})
            if stats.get("annotated_videos", 0) == 0:
                reporter.log(f"  Skipping {study_name}: no annotated videos (no prediction targets).")
                continue

            recoded_filename = f"{study_name}_recoded.parquet"
            if not data_io.exists(storage_location="cache", filename=recoded_filename):
                reporter.log(f"  Skipping {study_name}: no recoded parquet in cache.")
                continue

            df = data_io.load_parquet(storage_location="cache", filename=recoded_filename)
            windows, target_index, eligibility = sequence_analysis.prepare_window_table(
                df, window_n=window_n, session_gap_s=session_gap_s
            )
            summary = sequence_analysis.compute_summary(
                windows, target_index, eligibility,
                window_n=window_n, session_gap_s=session_gap_s,
            )

            # Persist the window frame (dwell_bin cast to string for portable parquet).
            windows_out = windows.copy()
            if "dwell_bin" in windows_out.columns:
                windows_out["dwell_bin"] = windows_out["dwell_bin"].astype(str)
            data_io.save_parquet(
                df=windows_out,
                storage_location="cache",
                filename=f"{study_name}_sequence.parquet",
            )
            data_io.save_json(
                data=summary,
                storage_location="cache",
                filename=f"{study_name}_sequence_summary.json",
            )

            n_elig = summary["metadata"]["n_participants_eligible"]
            reporter.log(
                f"  Refreshed sequence for {study_name}: {len(windows)} windows, "
                f"{n_elig} eligible participant(s), {len(target_index)} targets."
            )

        except Exception as e:
            reporter.log(f"Error processing {study_name}: {e}")

        _t_study = time.perf_counter() - _t_study_start
        reporter.log(f"  [TIMING] study={study_name} total={_t_study:.2f}s")
        reporter.update_progress(int(((i + 1) / total) * 100), f"Done {i + 1}/{total}")

    _t_run = time.perf_counter() - _t_run_start
    reporter.log(f"[TIMING] sequence_refresh wall={_t_run:.2f}s studies={total}")
    reporter.log("Sequence Analysis refresh completed.")




if __name__ == "__main__":
    from web_interface.worker_runner import run_worker

    def _make_task_args(args) -> dict:
        task_args: dict = {}
        if args.studies:
            task_args["studies"] = args.studies
        elif args.study_name:
            task_args["studies"] = args.study_name
        if args.window_n is not None:
            task_args["window_n"] = args.window_n
        if args.session_gap_s is not None:
            task_args["session_gap_s"] = args.session_gap_s
        return task_args

    run_worker(
        run_sequence_refresh,
        "sequence_refresh",
        arg_specs=[
            (("--studies",), {"type": str, "default": None,
                              "help": "Comma-separated study names to refresh (default: all)"}),
            (("--window-n",), {"type": int, "default": None,
                               "help": "Videos per sequence window "
                                       "(default: sequence_analysis.DEFAULT_WINDOW_N)"}),
            (("--session-gap-s",), {"type": int, "default": None,
                                    "help": "Idle seconds that end a session "
                                            "(default: sequence_analysis.SESSION_GAP_S)"}),
            (("study_name",), {"nargs": "?", "default": None,
                               "help": "Single study to refresh; ignored when --studies is given"}),
        ],
        make_task_args=_make_task_args,
        description="Refresh sequence-analysis artifacts (dwell -> next-window lift)",
    )
