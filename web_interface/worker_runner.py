"""Shared ``__main__`` boilerplate for the ``run_*.py`` background workers.

Each worker's subprocess entry point is the same shape: build an argparse
parser, derive ``task_args`` from the parsed CLI args, run the worker's
``run_<name>(reporter, task_args)`` function under a ``LocalStatusReporter``,
and translate an exception into ``reporter.fail`` + exit code 1.
``run_worker`` centralizes that shape; each worker keeps its own arg specs and
``task_args`` construction so CLI behavior is unchanged.

Workers whose entry points genuinely deviate (queue loops with their own
chaining, dynamic reporter names, manual ``sys.argv`` handling) keep their
bespoke ``__main__`` blocks and do not use this helper.
"""

import argparse
import sys
from collections.abc import Callable

from web_interface.task_status import LocalStatusReporter, TaskStatusReporter

ArgSpec = tuple[tuple, dict]






def run_worker(
    run_fn: Callable[..., object],
    name: str,
    arg_specs: list[ArgSpec] | None = None,
    make_task_args: Callable[[argparse.Namespace], dict] | None = None,
    description: str | None = None,
) -> None:
    """Run a worker's ``run_<name>`` function as a local subprocess.

    Args:
        run_fn: The worker's ``run_<name>(reporter, task_args)`` function.
            Any chain-dispatch return value is intentionally ignored — in
            subprocess mode the workers that chain handle continuation
            server-side or not at all, matching the pre-helper behavior.
        name: Process name passed to ``LocalStatusReporter``.
        arg_specs: ``(args, kwargs)`` pairs forwarded verbatim to
            ``ArgumentParser.add_argument``.
        make_task_args: Builds the ``task_args`` dict from the parsed args.
            Defaults to an empty dict.
        description: Optional ``ArgumentParser`` description.
    """
    parser = argparse.ArgumentParser(description=description)
    for spec_args, spec_kwargs in arg_specs or []:
        parser.add_argument(*spec_args, **spec_kwargs)
    args = parser.parse_args()

    task_args = make_task_args(args) if make_task_args else {}

    reporter: TaskStatusReporter = LocalStatusReporter(name)
    try:
        run_fn(reporter=reporter, task_args=task_args)
        reporter.complete()
    except Exception as e:
        reporter.fail(str(e))
        sys.exit(1)
