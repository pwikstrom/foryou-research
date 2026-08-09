"""Central logging configuration for the fyp package.

Provides `get_logger()`, which returns a logger writing bare messages to
STDOUT. Workers launched as subprocesses have their stdout parsed line-by-line
by `web_interface/process_manager.py` and shown as UI log lines, so all
diagnostic output must go to stdout with a plain "%(message)s" format —
byte-identical to what `print()` produced before the logging migration.

This module must stay import-free of `fyp.fyp_config` (and anything else with
import side effects) so any module can import it without triggering config
initialization.
"""

import logging
import os
import sys
import threading

_HANDLER_FLAG = "_fyp_stdout_handler"

DEFAULT_LEVEL = "INFO"

# Every logger `get_logger` has handed out, plus the extra sinks attached to
# them. A registry is required because these loggers set `propagate = False`
# (so output stays on the single stdout handler and never doubles up through
# the root logger) — which also means a handler added to the root logger would
# never see a single fyp record. `add_sink` is the supported way in.
_LOGGERS: list[logging.Logger] = []
_SINKS: list[logging.Handler] = []
_REGISTRY_LOCK = threading.Lock()




def _resolve_level() -> int:
    """Resolve the log level from the FYP_LOG_LEVEL environment variable.

    Returns:
        The numeric logging level named by ``FYP_LOG_LEVEL`` (case-insensitive
        standard level name, e.g. ``DEBUG``/``INFO``/``WARNING``/``ERROR``),
        falling back to ``INFO`` when unset or unrecognised.
    """
    name = os.environ.get("FYP_LOG_LEVEL", DEFAULT_LEVEL).strip().upper()
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        level = logging.INFO
    return level




def get_logger(name: str) -> logging.Logger:
    """Return a logger with an idempotently-configured stdout handler.

    The handler writes to ``sys.stdout`` with a bare ``%(message)s`` format so
    that subprocess-mode UI log lines stay identical to plain ``print()``
    output. Calling this repeatedly with the same name never attaches a second
    handler.

    Args:
        name: Logger name, conventionally the calling module's ``__name__``.

    Returns:
        A configured ``logging.Logger`` with propagation disabled.
    """
    logger = logging.getLogger(name)
    if not any(getattr(h, _HANDLER_FLAG, False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        setattr(handler, _HANDLER_FLAG, True)
        logger.addHandler(handler)
        logger.setLevel(_resolve_level())
        logger.propagate = False
    with _REGISTRY_LOCK:
        if logger not in _LOGGERS:
            _LOGGERS.append(logger)
        for sink in _SINKS:
            if sink not in logger.handlers:
                logger.addHandler(sink)
    return logger




def add_sink(handler: logging.Handler) -> None:
    """Attach an extra handler to every fyp logger, now and in future.

    The task runner uses this to tee worker output into the durable process log
    shown in the web UI: on Cloud Run the worker is not a subprocess of the web
    service, so its stdout goes only to Cloud Logging and the UI would otherwise
    show nothing but explicit ``reporter.log`` calls.

    Args:
        handler: The handler to attach. Attaching twice is a no-op.
    """
    with _REGISTRY_LOCK:
        if handler not in _SINKS:
            _SINKS.append(handler)
        for logger in _LOGGERS:
            if handler not in logger.handlers:
                logger.addHandler(handler)




def remove_sink(handler: logging.Handler) -> None:
    """Detach a handler previously attached with ``add_sink``.

    Args:
        handler: The handler to detach. Detaching an unattached handler is a
            no-op, so this is safe to call from a ``finally`` block.
    """
    with _REGISTRY_LOCK:
        if handler in _SINKS:
            _SINKS.remove(handler)
        for logger in _LOGGERS:
            if handler in logger.handlers:
                logger.removeHandler(handler)
