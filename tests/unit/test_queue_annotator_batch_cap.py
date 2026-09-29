"""The synchronous annotator's batch cap fits the Cloud Tasks deadline.

Each chain link must finish inside ``CLOUD_TASKS_MAX_DISPATCH_DEADLINE``
(1800 s). The cap used to be 2,000 videos against an old 3,600 s figure, so
batches of 667-2,000 passed validation but were estimated to overrun.
"""

import pytest


class _Reporter:
    def __init__(self):
        self.lines = []

    def log(self, msg):
        self.lines.append(msg)

    def update_progress(self, *_a, **_k):
        pass


def test_cap_is_the_largest_batch_whose_estimate_fits_the_deadline():
    from web_interface.tasks import worker_registry
    from web_interface.workers import run_queue_annotator as rqa

    deadline = worker_registry.CLOUD_TASKS_MAX_DISPATCH_DEADLINE
    per_video = rqa._SECONDS_PER_VIDEO / rqa._WORKERS * rqa._SAFETY_MARGIN
    assert rqa.MAX_BATCH_SIZE * per_video <= deadline
    assert (rqa.MAX_BATCH_SIZE + 1) * per_video > deadline
    assert rqa.MAX_BATCH_SIZE == 666


def test_batch_over_the_cap_is_rejected_before_any_work(monkeypatch):
    import fyp.core.data_io as data_io
    from web_interface.workers import run_queue_annotator as rqa

    monkeypatch.setattr(rqa, "_estimate_seconds", lambda n: 0.0)
    touched = []
    monkeypatch.setattr(data_io, "exists", lambda **kw: touched.append(kw) or False)

    with pytest.raises(ValueError, match="1800s Cloud Tasks deadline"):
        rqa.run_queue_annotator(_Reporter(), {"batch_size": rqa.MAX_BATCH_SIZE + 1})
    assert touched == []

    # At the cap the run proceeds (and stops at the empty queue).
    assert rqa.run_queue_annotator(_Reporter(), {"batch_size": rqa.MAX_BATCH_SIZE}) is None
    assert touched
