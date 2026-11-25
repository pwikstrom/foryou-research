"""Several copies of the local Qwen model side by side, one per child process.

One copy of the model leaves the graphics processor idle for part of every
video (while the input is prepared on a single CPU core). A second copy fills
those gaps: measured on an M5 Max with the 8-bit model, two copies process
about 1.4 times as many videos per hour as one. A third copy did not fit the
graphics memory. The number is ``[machine.qwen_local] copies``.

Each copy is a separate Python process that loads its own model and answers
``annotate_one`` requests over a local socket. The parent keeps the ordinary
backend interface: the orchestrator's thread pool is ``copies`` wide, and each
thread hands its item to whichever copy is free. A copy that crashes, hangs or
reports a graphics-processor error is stopped and started afresh for the next
item (a Metal error leaves a process unable to run the model again).

Child entry point: ``python -m fyp.annotation.backends.qwen_copies``.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Client, Listener
from pathlib import Path

from fyp.core.logging_setup import get_logger

logger = get_logger(__name__)

# Set in a child so its own backend runs the model in-process instead of
# starting copies of its own.
CHILD_ENV = "FYP_QWEN_LOCAL_COPY"
_ADDRESS_ENV = "FYP_QWEN_LOCAL_COPY_ADDRESS"
_KEY_ENV = "FYP_QWEN_LOCAL_COPY_KEY"

# Time allowed for a copy to start Python, import the Hub and connect back.
START_TIMEOUT_SECONDS = 300
# One video: preparation, speech transcript and a full-length answer take well
# under a minute, and the first one adds the model load (about a minute from a
# cold disk); ten minutes means the copy is stuck.
ITEM_TIMEOUT_SECONDS = 600


def is_child() -> bool:
    """Whether this process is one of the model copies."""
    return os.environ.get(CHILD_ENV) == "1"


def _child_env(address: str, key: str) -> dict[str, str]:
    """Environment for a copy: same config file and same code tree as the parent."""
    import fyp
    from fyp.core.fyp_config import PROJECT_ROOT, active_config_path

    env = os.environ.copy()
    env[CHILD_ENV] = "1"
    env[_ADDRESS_ENV] = address
    env[_KEY_ENV] = key
    env["FYP_CONFIG_PATH"] = active_config_path()
    roots = [str(Path(PROJECT_ROOT).resolve())]
    fyp_root = Path(fyp.__file__).resolve().parent.parent
    if str(fyp_root) not in roots and (fyp_root / "__proj__.py").exists():
        roots.append(str(fyp_root))
    inherited = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(roots + [p for p in inherited if p not in roots])
    return env


class _Copy:
    """One child process holding one model."""

    def __init__(self, index: int, overrides: dict):
        self.index = index
        self._overrides = overrides
        self._proc: subprocess.Popen | None = None
        self._conn = None

    def _start(self) -> None:
        key = os.urandom(16).hex()
        listener = Listener(family="AF_UNIX", authkey=key.encode())
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "fyp.annotation.backends.qwen_copies"],
                env=_child_env(listener.address, key),
                stdin=subprocess.DEVNULL,
            )
            # accept() has no timeout of its own, so it runs in a helper
            # thread while this one watches for a child that died first.
            accepted: dict = {}
            waiter = threading.Thread(
                target=lambda: accepted.setdefault("conn", listener.accept()), daemon=True
            )
            waiter.start()
            deadline = time.monotonic() + START_TIMEOUT_SECONDS
            while waiter.is_alive():
                waiter.join(0.5)
                if self._proc.poll() is not None and waiter.is_alive():
                    raise OSError(
                        f"copy exited with code {self._proc.returncode} before connecting"
                    )
                if time.monotonic() > deadline:
                    raise TimeoutError("copy did not connect in time")
            self._conn = accepted["conn"]
        finally:
            listener.close()
        self._conn.send({"overrides": self._overrides})
        logger.info(f"Local Qwen copy {self.index} started (pid {self._proc.pid})")

    def stop(self) -> None:
        """Stop the child; the next request starts a fresh one."""
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait(timeout=30)
            self._proc = None

    def run(self, request: dict) -> dict | None:
        """Send one request and wait for its row; None when the copy failed."""
        try:
            if self._proc is None or self._proc.poll() is not None:
                self.stop()
                self._start()
            self._conn.send(request)
            if not self._conn.poll(ITEM_TIMEOUT_SECONDS):
                logger.warning(f"Local Qwen copy {self.index} timed out; restarting it")
                self.stop()
                return None
            row = self._conn.recv()
        except (EOFError, OSError, TimeoutError) as exc:
            logger.warning(f"Local Qwen copy {self.index} failed ({exc!r}); restarting it")
            self.stop()
            return None
        if "METAL" in str(row.get("error", "")):
            logger.warning(f"Local Qwen copy {self.index} hit a GPU error; restarting it")
            self.stop()
        return row


class CopyPool:
    """A fixed number of model copies; ``run`` blocks until one is free."""

    def __init__(self, copies: int, overrides: dict):
        self.signature = (int(copies), json.dumps(overrides, sort_keys=True, default=str))
        self._idle: queue.Queue[_Copy] = queue.Queue()
        self._all = [_Copy(i, overrides) for i in range(int(copies))]
        for copy in self._all:
            self._idle.put(copy)

    def run(self, request: dict) -> dict | None:
        """Annotate one item on the next free copy.

        Args:
            request: The keyword arguments of ``annotate_one``.

        Returns:
            The raw-row dict, or None when the copy crashed or timed out
            (the caller turns that into a failure row).
        """
        copy = self._idle.get()
        try:
            return copy.run(request)
        finally:
            self._idle.put(copy)

    def stop(self) -> None:
        """Stop every copy (used by tests and at shutdown)."""
        for copy in self._all:
            copy.stop()


_pool: CopyPool | None = None
_pool_lock = threading.Lock()


def get_pool(copies: int, overrides: dict) -> CopyPool:
    """The process-wide pool, created on first use.

    Args:
        copies: How many model copies to run.
        overrides: Backend overrides every copy applies (``copies`` itself is
            forced to 1 inside a child).

    Returns:
        The pool.

    Raises:
        RuntimeError: When a pool with a different size or model settings is
            already running in this process.
    """
    global _pool
    overrides = {**overrides, "copies": 1}
    with _pool_lock:
        signature = (int(copies), json.dumps(overrides, sort_keys=True, default=str))
        if _pool is None:
            _pool = CopyPool(copies, overrides)
        elif _pool.signature != signature:
            raise RuntimeError(
                "local Qwen copies are already running with different settings; "
                "restart the worker to change the model or the number of copies"
            )
        return _pool


def _child_main() -> None:
    """Serve ``annotate_one`` requests until the parent goes away."""
    conn = Client(os.environ[_ADDRESS_ENV], authkey=os.environ[_KEY_ENV].encode())
    from fyp.annotation.backends.qwen_local import QwenLocalBackend

    backend = QwenLocalBackend(overrides=conn.recv()["overrides"])
    while True:
        try:
            request = conn.recv()
        except EOFError:
            return
        conn.send(backend.annotate_one(**request))


if __name__ == "__main__":
    _child_main()
