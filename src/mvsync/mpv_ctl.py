"""Adapter around a project-managed mpv instance driven over JSON IPC.

The controller starts mpv with the project-local config directory (so global
mpv settings never leak in) and exposes the few properties the sync loop
needs. Everything else in the codebase talks to this class, not the IPC
library, which keeps the coupling in one place.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from mvsync import paths

IPC_SOCKET = "mvsync-mpv"


class MpvError(RuntimeError):
    pass


class MpvController:
    def __init__(self, root: Path | None = None, ipc_socket: str = IPC_SOCKET) -> None:
        self._root = root
        self._ipc_socket = ipc_socket
        self._mpv = None

    def start(self) -> None:
        from python_mpv_jsonipc import MPV

        mpv_path = paths.find_tool("mpv", self._root)
        if mpv_path is None:
            raise MpvError("mpv not found; run bootstrap.py or install it")
        config_dir = paths.ensure_dir(paths.mpv_config_dir(self._root))
        extra_args: dict[str, object] = {}
        ytdlp = paths.find_tool("yt-dlp", self._root)
        if ytdlp is not None:
            extra_args["script_opts"] = [f"ytdl_hook-ytdl_path={ytdlp}"]
        self._mpv = MPV(
            ipc_socket=self._ipc_socket,
            mpv_location=str(mpv_path),
            config_dir=str(config_dir),
            idle=True,
            keep_open=True,
            mute=True,
            terminal="no",
            auto_window_resize=False,
            keepaspect_window=False,
            **extra_args,
        )

    def _require(self):
        if self._mpv is None:
            raise MpvError("mpv is not started")
        return self._mpv

    def load(self, url: str) -> None:
        self._require().loadfile(url, "replace")

    @property
    def time_pos(self) -> float | None:
        value = self._require().time_pos
        return float(value) if value is not None else None

    @property
    def duration(self) -> float | None:
        value = self._require().duration
        return float(value) if value is not None else None

    @property
    def path(self) -> str:
        value = self._require().path
        return str(value) if value is not None else ""

    @property
    def paused(self) -> bool:
        return bool(self._require().pause)

    @property
    def fullscreen(self) -> bool:
        return bool(self._require().fullscreen)

    @property
    def eof_reached(self) -> bool:
        return bool(self._require().eof_reached)

    def seek_absolute(self, seconds: float) -> None:
        self._require().seek(float(seconds), "absolute")

    def set_rate(self, rate: float) -> None:
        self._require().speed = float(rate)

    def set_muted(self, muted: bool) -> None:
        self._require().mute = bool(muted)

    def set_paused(self, paused: bool) -> None:
        self._require().pause = bool(paused)

    def set_fullscreen(self, fullscreen: bool) -> None:
        self._require().fullscreen = bool(fullscreen)

    def stop(self) -> None:
        if self._mpv is None:
            return
        with contextlib.suppress(Exception):
            self._mpv.quit()
        self._mpv = None
