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

``chain_locally`` is the local counterpart of Cloud Tasks self-chaining: a
worker that returns ``{"chain": True, "next_task_args": ...}`` has its next
link run in the same process.
"""

import argparse
import sys
import time
from collections.abc import Callable

from web_interface.tasks.task_status import LocalStatusReporter, TaskStatusReporter

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
            Its return value is ignored; a worker that self-chains passes
            ``functools.partial(chain_locally, run_<name>)`` instead.
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


def chain_locally(
    run_fn: Callable[..., dict | None],
    reporter: TaskStatusReporter,
    task_args: dict,
    *,
    honour_delay: bool = False,
) -> int:
    """Run every link of a self-chaining worker in this process.

    On Cloud Run each link returns ``{"chain": True, "next_task_args": ...}``
    and the runtime dispatches the next one as a new Cloud Task. Locally there
    is no queue, so this calls ``run_fn`` again with the next link's
    ``task_args`` until a link returns no chain.

    Args:
        run_fn: The worker's ``run_<name>(reporter, task_args)`` function.
        reporter: The status reporter every link reports through.
        task_args: The first link's ``task_args``.
        honour_delay: Sleep for a link's ``next_dispatch_delay_seconds``
            before the next one, as the Cloud Tasks schedule would (a poll
            loop would otherwise spin).

    Returns:
        The number of links run.
    """
    links = 0
    while True:
        result = run_fn(reporter=reporter, task_args=task_args)
        links += 1
        if not result or not result.get("chain"):
            return links
        delay = result.get("next_dispatch_delay_seconds")
        if honour_delay and delay:
            time.sleep(delay)
        task_args = result["next_task_args"]
