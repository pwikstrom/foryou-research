"""Verify [IO] timing lines are off by default, and the fyp logging sink registry.

``data_io._io_log`` fires on every read and write — including the status writes
a running task makes every few seconds — so at INFO it drowned the worker's own
output in the UI log. It now logs at DEBUG, recoverable via FYP_LOG_LEVEL=DEBUG.

The sink registry is the other half: ``get_logger`` sets ``propagate = False``,
so a handler on the root logger never sees an fyp record. ``add_sink`` is how
the Cloud Run task runner tees worker narration into the durable process log.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fyp.data_io as data_io
from fyp.core import logging_setup


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def sink():
    handler = _Capture()
    logging_setup.add_sink(handler)
    yield handler
    logging_setup.remove_sink(handler)






def test_io_lines_are_silent_at_info(sink):
    logger = data_io.logger
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        data_io._io_log("load_json", "cache", "thing.json", "local", 12, 3.4)
    finally:
        logger.setLevel(previous)

    assert not [m for m in sink.messages if m.startswith("[IO]")]


def test_io_lines_return_at_debug(sink):
    logger = data_io.logger
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        data_io._io_log("load_json", "cache", "thing.json", "local", 12, 3.4)
    finally:
        logger.setLevel(previous)

    io_lines = [m for m in sink.messages if m.startswith("[IO]")]
    assert len(io_lines) == 1
    assert "op=load_json" in io_lines[0] and "file=thing.json" in io_lines[0]






def test_add_sink_reaches_loggers_created_after_it(sink):
    logger = logging_setup.get_logger("fyp.test.created_later")
    logger.info("narration from a later import")
    assert "narration from a later import" in sink.messages


def test_remove_sink_detaches_from_every_logger():
    handler = _Capture()
    logging_setup.add_sink(handler)
    logger = logging_setup.get_logger("fyp.test.detach")
    logging_setup.remove_sink(handler)

    logger.info("after detach")
    assert handler.messages == []
    assert handler not in logger.handlers


def test_sink_never_displaces_the_stdout_handler(capsys, sink):
    # The subprocess path parses worker stdout line-by-line; a sink must be
    # additive or local-mode logs would go dark.
    logger = logging_setup.get_logger("fyp.test.stdout_intact")
    logger.info("still on stdout")
    assert "still on stdout" in capsys.readouterr().out
    assert "still on stdout" in sink.messages


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
