"""Opt-in developer log: one JSON-lines file per run, off unless enabled.

Switch it on with ``{"dev_log": true}`` in ``data/config.json`` or with
``MVSYNC_DEV_LOG=1``. Files land under ``data/logs/``; deleting them is safe.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from mvsync import config, paths

ENV_VAR = "MVSYNC_DEV_LOG"
_TRUTHY = ("1", "true", "yes", "on")

_lock = threading.Lock()
_writer: _Writer | None = None


class _Writer:
    """Append-only JSON-lines file shared by the loop and its worker threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, line: str) -> None:
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            self._handle.close()


def enabled(root: Path | None = None) -> bool:
    """The environment override wins; otherwise the config file decides."""
    if os.environ.get(ENV_VAR, "").strip().lower() in _TRUTHY:
        return True
    return config.load_dev_log(root)


def configure(root: Path | None = None) -> Path | None:
    """Open this run's log file when enabled; safe to call more than once."""
    global _writer
    with _lock:
        if _writer is not None:
            return _writer.path
        if not enabled(root):
            return None
        log_dir = paths.ensure_dir(paths.logs_dir(root))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = log_dir / f"mvsync-{stamp}-{os.getpid()}.log"
        _writer = _Writer(path)
        return path


def log(event: str, /, **fields: Any) -> None:
    """Write one structured event; a no-op unless configure() opened a file."""
    writer = _writer
    if writer is None:
        return
    payload = {"t": time.time(), "event": event, **fields}
    with contextlib.suppress(OSError, TypeError, ValueError):
        writer.write(json.dumps(payload, ensure_ascii=False, default=str))


def close() -> None:
    """Flush and forget the file; later ``log`` calls are no-ops."""
    global _writer
    with _lock:
        writer, _writer = _writer, None
    if writer is not None:
        writer.close()
