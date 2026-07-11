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

_HANDLER_FLAG = "_fyp_stdout_handler"

DEFAULT_LEVEL = "INFO"




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
    return logger
