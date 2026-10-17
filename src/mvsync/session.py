"""Single-session guard: one mvsync session at a time per project folder.

``play``, ``scan``, and ``fix`` claim ``data/session.lock`` (holding the pid of
the process) before touching Spotify, the loopback device, mpv, or the
recordings. A stale lock left behind by a crashed session is stolen automatically.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
from pathlib import Path

from mvsync import paths

LOCK_NAME = "session.lock"


def _lock_path(root: Path | None = None) -> Path:
    return paths.data_dir(root) / LOCK_NAME


def _pid_alive(pid: int) -> bool:
    """True when the pid names a running process (Windows process handles)."""
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def claim(root: Path | None = None) -> None:
    """Claim the session lock, refusing when another live session holds it."""
    lock = _lock_path(root)
    if lock.is_file():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            raise RuntimeError(
                f"another session is already running (pid {pid}); close it first, "
                f"or delete data/{LOCK_NAME} if it is stale"
            )
    paths.ensure_dir(lock.parent)
    lock.write_text(str(os.getpid()), encoding="utf-8")


def release(root: Path | None = None) -> None:
    """Release the lock when this process owns it."""
    lock = _lock_path(root)
    with contextlib.suppress(OSError):
        if lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink(missing_ok=True)
