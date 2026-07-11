"""Stdout protocol contract for subprocess-mode status reporters.

`web_interface/process_manager.py` (enqueue_output) parses worker subprocess
STDOUT line-by-line for the literal `::PROGRESS::` / `::DATA::` markers; every
other stdout line becomes a UI log line. The print() calls emitting these
markers in `web_interface/task_status.py` must therefore never be converted to
logging or moved off stdout. This test pins the exact wire format.
"""

from __future__ import annotations

import json

import pytest

from web_interface.task_status import LocalStatusReporter, LocalThreadStatusReporter




def _marker_lines(captured_out: str, marker: str) -> list[str]:
    return [line for line in captured_out.splitlines() if marker in line]




def test_local_reporter_progress_line_format(capsys: pytest.CaptureFixture) -> None:
    reporter = LocalStatusReporter("contract_test")
    reporter.update_progress(42, "Halfway there")
    out = capsys.readouterr().out

    lines = _marker_lines(out, "::PROGRESS::")
    assert lines == ['::PROGRESS::{"percent": 42, "message": "Halfway there"}']
    # The marker must start the line (no logging prefix) and the remainder must
    # parse exactly the way process_manager splits it.
    line = lines[0]
    assert line.startswith("::PROGRESS::")
    _, json_str = line.split("::PROGRESS::", 1)
    assert json.loads(json_str.strip()) == {"percent": 42, "message": "Halfway there"}




def test_local_reporter_progress_with_stage_fields(capsys: pytest.CaptureFixture) -> None:
    reporter = LocalStatusReporter("contract_test")
    reporter.update_progress(10, "Stage run", stage_index=1, stage_total=3, stage_name="recode")
    out = capsys.readouterr().out

    lines = _marker_lines(out, "::PROGRESS::")
    assert len(lines) == 1
    _, json_str = lines[0].split("::PROGRESS::", 1)
    assert json.loads(json_str.strip()) == {
        "percent": 10,
        "message": "Stage run",
        "stage_index": 1,
        "stage_total": 3,
        "stage_name": "recode",
    }




def test_local_reporter_data_line_format(capsys: pytest.CaptureFixture) -> None:
    reporter = LocalStatusReporter("contract_test")
    reporter.emit_data({"annotate_queue_len": 7})
    out = capsys.readouterr().out

    lines = _marker_lines(out, "::DATA::")
    assert lines == ['::DATA::{"annotate_queue_len": 7}']
    _, json_str = lines[0].split("::DATA::", 1)
    assert json.loads(json_str.strip()) == {"annotate_queue_len": 7}




def test_local_thread_reporter_emits_same_wire_format(capsys: pytest.CaptureFixture) -> None:
    reporter = LocalThreadStatusReporter("contract_test_thread")
    reporter.update_progress(55, "Thread progress")
    reporter.emit_data({"rows": 3})
    out = capsys.readouterr().out

    progress_lines = _marker_lines(out, "::PROGRESS::")
    data_lines = _marker_lines(out, "::DATA::")
    assert progress_lines == ['::PROGRESS::{"percent": 55, "message": "Thread progress"}']
    assert data_lines == ['::DATA::{"rows": 3}']




def test_plain_log_lines_stay_bare(capsys: pytest.CaptureFixture) -> None:
    reporter = LocalStatusReporter("contract_test")
    reporter.start()
    reporter.log("plain log line")
    out = capsys.readouterr().out.splitlines()

    assert "[contract_test] Starting..." in out
    assert "plain log line" in out




if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
