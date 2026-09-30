"""``chain_locally`` runs a self-chaining worker's links in one process.

The embeddings, sessions and batch-annotation workers each used to carry
their own copy of this loop in ``__main__``.
"""

import web_interface.tasks.worker_runner as worker_runner
from web_interface.tasks.worker_runner import chain_locally


def _worker(links: int, delay=None):
    seen = []

    def run(reporter, task_args):
        seen.append(dict(task_args))
        n = task_args.get("n", 0)
        if n + 1 >= links:
            return None
        out = {"chain": True, "next_task_args": {**task_args, "n": n + 1}}
        if delay is not None:
            out["next_dispatch_delay_seconds"] = delay
        return out

    return run, seen


def test_follows_next_task_args_until_a_link_returns_no_chain():
    run, seen = _worker(3)
    assert chain_locally(run, object(), {"batch_size": 5}) == 3
    assert seen == [{"batch_size": 5}, {"batch_size": 5, "n": 1}, {"batch_size": 5, "n": 2}]


def test_a_result_without_chain_true_ends_the_run():
    calls = []

    def run(reporter, task_args):
        calls.append(task_args)
        return {"chain": False, "next_task_args": {"never": True}}

    assert chain_locally(run, object(), {}) == 1
    assert calls == [{}]


def test_the_dispatch_delay_is_slept_only_when_asked(monkeypatch):
    slept = []
    monkeypatch.setattr(worker_runner.time, "sleep", slept.append)
    run, _ = _worker(3, delay=120)
    chain_locally(run, object(), {})
    assert slept == []
    run, _ = _worker(3, delay=120)
    chain_locally(run, object(), {}, honour_delay=True)
    assert slept == [120, 120]
