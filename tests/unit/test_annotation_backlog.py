"""The annotation backlog: claims, giving way to the hosted annotator, and the worker loop.

Runs against real ``data_io`` local-mode files in a temporary folder, so the
compare-and-swap in ``update_json`` is the real one.
"""

import os
import threading
from datetime import timedelta

import pytest

import fyp.core.data_io as data_io
from web_interface.services import annotation_backlog as backlog
from web_interface.workers import backlog_annotator as worker


@pytest.fixture
def store(tmp_path, monkeypatch):
    def _resolve(storage_location="cache", filename=""):
        return (os.path.join(tmp_path, filename), None, "local", None)

    monkeypatch.setattr(data_io, "resolve_paths", _resolve)
    # No status table in these tests: every listed id counts as eligible.
    monkeypatch.setattr(backlog, "eligible_ids", lambda ids=None, retry_ids=(): list(ids or []))
    return tmp_path


def _ids(n, start=0):
    return [f"70000000000000{i:05d}" for i in range(start, start + n)]


def test_claims_do_not_overlap_and_finished_ids_leave(store):
    backlog.set_backlog(_ids(10))
    a = backlog.claim(4)
    b = backlog.claim(4)
    assert a["items"] == _ids(4) and b["items"] == _ids(4, 4)
    assert backlog.claimed_ids() == set(_ids(8))
    # A claim removes nothing: a worker that dies loses no ids.
    assert backlog.load_backlog()["items"] == _ids(10)

    left = backlog.complete(a["claim_id"], ok_ids=a["items"][:3], fail_ids=a["items"][3:])
    assert left == 6
    state = backlog.load_backlog()
    assert state["items"] == _ids(6, 4) and state["failed"] == _ids(1, 3)
    assert backlog.claimed_ids() == set(_ids(4, 4))


def test_an_abandoned_claim_returns_its_ids(store, monkeypatch):
    backlog.set_backlog(_ids(4))
    first = backlog.claim(4)
    assert backlog.claim(4) is None  # all spoken for
    later = backlog._now() + timedelta(seconds=backlog.CLAIM_STALE_S + 5)
    monkeypatch.setattr(backlog, "_now", lambda: later)
    again = backlog.claim(4)
    assert again["items"] == first["items"] and again["claim_id"] != first["claim_id"]
    assert backlog.heartbeat(first["claim_id"]) is False  # the stale claim was cleared


def test_released_claim_keeps_its_ids_listed(store):
    backlog.set_backlog(_ids(3))
    c = backlog.claim(3)
    backlog.release(c["claim_id"])
    assert backlog.claimed_ids() == set()
    assert backlog.claim(3)["items"] == _ids(3)


def test_backlog_gives_way_to_the_hosted_annotator(store):
    ids = _ids(6)
    backlog.set_backlog(ids)
    data_io.save_json(data=[ids[0]], storage_location="cache", filename="to_annotate.json")
    data_io.save_json(
        data={"jobs": [{"submitted_ids": [ids[1]]}]},
        storage_location="cache",
        filename="annotate_batch_job.json",
    )
    assert backlog.claim(3)["items"] == ids[2:5]
    # Passed over, not removed: they leave once enrichment status shows them annotated.
    assert backlog.load_backlog()["items"] == ids


def test_ids_that_no_longer_need_annotating_are_dropped(store, monkeypatch):
    ids = _ids(5)
    backlog.set_backlog(ids)
    monkeypatch.setattr(
        backlog, "eligible_ids", lambda c=None, retry_ids=(): [i for i in c if i != ids[1]]
    )
    c = backlog.claim(3)
    assert c["items"] == [ids[0], ids[2], ids[3]] and c["dropped"] == [ids[1]]
    state = backlog.load_backlog()
    assert ids[1] not in state["items"] and state["failed"] == []


def test_relisting_keeps_failures_out(store):
    backlog.set_backlog(_ids(3))
    c = backlog.claim(3)
    backlog.complete(c["claim_id"], ok_ids=c["items"][:2], fail_ids=c["items"][2:])
    assert backlog.set_backlog(_ids(5)) == 4
    assert _ids(1, 2)[0] not in backlog.load_backlog()["items"]


def test_two_workers_on_one_backlog_annotate_no_video_twice(store, monkeypatch):
    """Gate 3: two workers race on one backlog; every id is annotated exactly once."""
    monkeypatch.setattr(backlog, "write_status", lambda **k: None)
    total = 400
    backlog.set_backlog(_ids(total))
    seen: list[str] = []
    lock = threading.Lock()

    def annotate(ids):
        with lock:
            seen.extend(ids)
        return ids, []

    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                worker.run(chunk=7, annotate=annotate, sleep=lambda s: None)
            )
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert sorted(seen) == _ids(total), "an id was annotated twice or missed"
    assert sum(r["ok"] for r in results) == total
    assert backlog.load_backlog()["items"] == [] and backlog.claimed_ids() == set()


def test_worker_stops_on_request_and_on_unreadable_chunks(store):
    backlog.set_backlog(_ids(30))
    asked = {"n": 0}

    def stop():
        asked["n"] += 1
        return asked["n"] > 2

    out = worker.run(chunk=5, annotate=lambda ids: (ids, []), stop_requested=stop)
    assert out["chunks"] == 2 and out["remaining"] == 20 and out["reason"] == "stopped by request"
    status = backlog.read_status()
    assert status["remaining"] == 20 and status["running"] is False
    assert status["worker"]["state"] == "stopped" and status["worker"]["ok"] == 10

    out = worker.run(chunk=5, annotate=lambda ids: ([], []))
    assert out["chunks"] == 0 and "nothing" in out["reason"]
    assert len(backlog.load_backlog()["items"]) == 20 and backlog.claimed_ids() == set()


def test_worker_releases_its_claim_when_annotation_raises(store):
    backlog.set_backlog(_ids(5))

    def boom(ids):
        raise RuntimeError("model crashed")

    with pytest.raises(RuntimeError):
        worker.run(chunk=5, annotate=boom)
    assert backlog.claimed_ids() == set() and len(backlog.load_backlog()["items"]) == 5
    assert backlog.read_status()["worker"]["state"] == "stopped"


def test_urgent_lane_goes_first_and_ids_move_up_not_down(store):
    rest, urgent = _ids(6), _ids(2, 4)
    backlog.set_backlog(rest)
    backlog.add(_ids(1, 2), lane="coverage")
    out = backlog.add(urgent, lane="urgent")
    assert out["added"] == 2 and out["lanes"] == {"urgent": 2, "coverage": 1, "rest": 3}
    # Taken in lane order; an id sits in one lane only.
    assert backlog.load_backlog()["items"] == urgent + _ids(1, 2) + _ids(2) + _ids(1, 3)
    # Adding an urgent id to a less urgent lane leaves it where it is.
    assert backlog.add(urgent, lane="rest")["added"] == 0
    assert backlog.claim(3)["items"] == urgent + _ids(1, 2)

    backlog.add(_ids(1, 3), lane="urgent", front=True)
    assert backlog.load_backlog()["lanes"]["urgent"][0] == _ids(1, 3)[0]


def test_first_format_backlog_is_read_as_the_last_lane(store):
    data_io.save_json(
        data={"version": 1, "items": _ids(3), "failed": _ids(1, 9)},
        storage_location="cache",
        filename=backlog.BACKLOG_FILENAME,
    )
    state = backlog.load_backlog()
    assert state["lanes"]["rest"] == _ids(3) and state["failed"] == _ids(1, 9)
    backlog.add(_ids(1, 5), lane="urgent")
    assert backlog.load_backlog()["items"] == _ids(1, 5) + _ids(3)


def test_remove_empty_and_pause(store):
    backlog.set_backlog(_ids(6))
    backlog.add(_ids(2, 6), lane="urgent")
    assert backlog.remove(_ids(1) + _ids(1, 6)) == 6
    backlog.empty("urgent")
    assert backlog.load_backlog()["items"] == _ids(5, 1)

    backlog.set_paused(True)
    assert backlog.claim(3) is None and backlog.read_status()["paused"] is True
    waited = []
    out = worker.run(
        chunk=3,
        annotate=lambda ids: (ids, []),
        sleep=lambda s: (waited.append(s), backlog.set_paused(False)),
    )
    assert waited == [worker.IDLE_WAIT_S] and out["ok"] == 5

    backlog.empty()
    assert backlog.load_backlog()["items"] == []


def test_failed_ids_are_listed_again_only_on_request(store, monkeypatch):
    backlog.set_backlog(_ids(2))
    c = backlog.claim(2)
    backlog.complete(c["claim_id"], ok_ids=[], fail_ids=c["items"])
    assert backlog.add(_ids(2), lane="urgent")["added"] == 0  # failures are not re-listed

    seen = {}
    monkeypatch.setattr(
        backlog,
        "eligible_ids",
        lambda ids=None, retry_ids=(): (seen.setdefault("retry", list(retry_ids)), list(ids))[1],
    )
    assert backlog.retry_failed() == 2
    state = backlog.load_backlog()
    assert state["items"] == _ids(2) and state["failed"] == [] and state["retry"] == _ids(2)
    c = backlog.claim(2)
    assert seen["retry"] == _ids(2)  # the earlier failure is ignored for these ids
    backlog.complete(c["claim_id"], ok_ids=c["items"], fail_ids=[])
    assert backlog.load_backlog()["retry"] == []


def test_worker_waits_for_new_videos_when_asked_to(store):
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            backlog.add(_ids(3), lane="urgent")

    stop = lambda: len(waits) >= 2  # noqa: E731
    out = worker.run(
        chunk=5,
        annotate=lambda ids: (ids, []),
        sleep=sleep,
        stop_requested=stop,
        wait_when_empty=True,
    )
    assert out["ok"] == 3 and out["reason"] == "stopped by request"


def test_status_text_and_commands(store, capsys):
    backlog.set_backlog(_ids(4))
    assert worker.main(["add", "--file", str(_write(store, _ids(2, 10))), "--front"]) == 0
    assert worker.main(["pause"]) == 0 and backlog.load_backlog()["paused"] is True
    assert worker.main(["resume"]) == 0 and worker.main(["status"]) == 0
    text = capsys.readouterr().out
    assert "6 video(s) left" in text and "urgent 2" in text and "has never reported" in text
    assert worker.main(["empty", "--lane", "urgent"]) == 0
    assert len(backlog.load_backlog()["items"]) == 4


def _write(folder, ids):
    path = folder / "ids.txt"
    path.write_text("\n".join(ids))
    return path
