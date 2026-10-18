"""Console plumbing for the GUI: an in-memory log sink and a key source."""

from __future__ import annotations

import threading
from collections import deque


class LogSink:
    """A stdout-compatible file object that buffers recent log lines for the GUI."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, max_lines: int = 2000) -> None:
        self._max_lines = max_lines
        self._text = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        """Append to the buffer, dropping the oldest lines past the cap."""
        with self._lock:
            self._text += text
            lines = self._text.splitlines(keepends=True)
            if len(lines) > self._max_lines:
                self._text = "".join(lines[max(0, len(lines) - self._max_lines) :])
        return len(text)

    def flush(self) -> None:
        """No-op: the buffer is already in memory."""

    def drain(self) -> list[str]:
        """Take every complete buffered line, leaving the buffer empty."""
        with self._lock:
            head, newline, _ = self._text.rpartition("\n")
            if not newline:
                return []
            self._text = ""
        return head.split("\n")

    @property
    def text(self) -> str:
        """The whole buffer, capped the same way drain sees it."""
        with self._lock:
            return self._text

    def isatty(self) -> bool:
        """Never a terminal: the GUI redraws instead of relying on cursor control."""
        return False

    def fileno(self) -> int:
        """No file descriptor: libraries inside a redirect must handle this."""
        return -1


class KeySource:
    """FIFO of keys pushed by the GUI, polled by the worker that owns the console."""

    def __init__(self) -> None:
        self._keys: deque[str] = deque()
        self._lock = threading.Lock()

    def push(self, key: str) -> None:
        """Queue one key for the next poll."""
        with self._lock:
            self._keys.append(key)

    def poll(self) -> str:
        """The oldest queued key, or "" when none is waiting."""
        with self._lock:
            if not self._keys:
                return ""
            return self._keys.popleft()
