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

from mvsync import devlog, paths

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


def lock_refusal_message(pid: int) -> str:
    """The one wording for a held lock, shared by ``claim`` and the GUI."""
    return (
        f"another session is already running (pid {pid}); close it first, "
        f"or delete data/{LOCK_NAME} if it is stale"
    )


def lock_holder(root: Path | None = None) -> int:
    """The live foreign pid holding the lock, or 0 when the lock is free.

    Reads only. Own pid counts as free: the GUI's own worker claims the lock
    under this process, so counting it would report our own session as foreign.
    """
    lock = _lock_path(root)
    if not lock.is_file():
        return 0
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    if pid and pid != os.getpid() and _pid_alive(pid):
        return pid
    return 0


def is_locked(root: Path | None = None) -> bool:
    """True when another live session holds the lock; never writes the lock file."""
    return lock_holder(root) != 0


def claim(root: Path | None = None) -> None:
    """Claim the session lock, refusing when another live session holds it."""
    lock = _lock_path(root)
    pid = lock_holder(root)
    if pid:
        devlog.log("lock_refused", pid=pid)
        raise RuntimeError(lock_refusal_message(pid))
    paths.ensure_dir(lock.parent)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    devlog.log("lock_claim", pid=os.getpid())


def release(root: Path | None = None) -> None:
    """Release the lock when this process owns it."""
    lock = _lock_path(root)
    with contextlib.suppress(OSError):
        if lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink(missing_ok=True)
            devlog.log("lock_release", pid=os.getpid())
