"""The worker registry is complete and self-consistent.

``web_interface/tasks/worker_registry.py`` is the one place a background worker is
declared; the process table, Cloud Tasks eligibility, dispatch deadlines, the
retry-safe set, the task-function table and the script paths are all derived
from it. These checks make a half-registered worker fail here rather than in
one of the two deployment modes.
"""

import inspect
from pathlib import Path

from web_interface.tasks import process_manager, runtime, worker_registry

WEB = Path(__file__).resolve().parents[2] / "web_interface"
# run_*.py files that are not workers.
NOT_WORKERS = {"run_logs.py"}


def test_every_worker_module_is_registered():
    on_disk = {p.name for p in WEB.glob("run_*.py")} - NOT_WORKERS
    registered = {spec.script.name for spec in worker_registry.WORKERS.values()}
    assert on_disk == registered, (
        f"unregistered: {sorted(on_disk - registered)}; missing files: {sorted(registered - on_disk)}"
    )


def test_every_worker_has_a_script_and_an_entry_point():
    functions = worker_registry.load_task_functions()
    for name, spec in worker_registry.WORKERS.items():
        assert spec.script.is_file(), f"{name}: no script at {spec.script}"
        fn = functions[name]
        assert fn.__name__ == spec.function_name and inspect.isfunction(fn), name


def test_deadlines_fit_cloud_tasks():
    for name, spec in worker_registry.WORKERS.items():
        if spec.deadline is not None:
            assert 15 <= spec.deadline <= worker_registry.CLOUD_TASKS_MAX_DISPATCH_DEADLINE, name


def test_derived_tables_agree_with_the_registry():
    workers = worker_registry.WORKERS
    assert process_manager.CLOUD_TASK_ELIGIBLE == set(workers)
    assert set(process_manager.processes) == {n for n, s in workers.items() if s.tracked}
    assert runtime.QUEUE_RETRY_SAFE == {n for n, s in workers.items() if s.retry_safe}
    assert set(process_manager.local_pipeline_script_map()) == {
        n for n, s in workers.items() if s.pipeline_step
    }
    for name, spec in workers.items():
        assert process_manager.dispatch_deadline_for(name) == spec.deadline, name
    # Only tracked workers have a card, so only they can be card-started.
    assert all(s.tracked for s in workers.values() if s.generic_start)


def test_scraper_workers_follow_the_scrape_contract():
    platforms = process_manager.scrape_platforms()
    assert worker_registry.SCRAPER_PROCESS_NAMES == [f"queue_scraper_{p}" for p in platforms]
