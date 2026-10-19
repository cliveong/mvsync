"""Command-line entry point: the main window, doctor, scan, play, fix, menu.

Heavy imports (winsdk, soundcard, librosa, tkinter, the runtimes) happen lazily
so ``--help`` stays fast.
"""

from __future__ import annotations

import argparse
import contextlib
import sys

from mvsync import paths

MENU = """
1) doctor            check environment, devices, and the player session
2) scan [url] [--pick N]   scan the current track (auto-finds its MV)
3) play [url] [--pick N]   play synced to the player (new tracks sync next time)
4) fix             pick the right MV for the current track (opens a window)
0) quit
""".strip()


def _doctor_capture_report() -> None:
    """Print the configured player and effective capture backend without raising."""
    try:
        from mvsync import config
        from mvsync.player_process import guess_player_exe

        mode = config.load_capture_mode()
        player = config.load_player()
        exe = config.load_player_exe()
        if not exe and player:
            exe = guess_player_exe(player)
        if not exe and not player:
            try:
                from mvsync.player_smtc import PlayerBridge

                auto_player = PlayerBridge().selected_app_id()
            except Exception:  # noqa: BLE001 - doctor reports, never raises
                auto_player = None
            if auto_player:
                player = auto_player
                exe = guess_player_exe(auto_player)
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        print(f"[doctor] capture: unavailable ({exc})")
        return
    player_detail = player or "none"
    exe_detail = exe or "none"

    try:
        from mvsync import capture_proc
    except ImportError as exc:
        print(
            f"[doctor] capture: mode={mode}, player={player_detail}, exe={exe_detail}, "
            f"process loopback unavailable ({exc}), backend=device loopback"
        )
        return
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        print(
            f"[doctor] capture: mode={mode}, player={player_detail}, exe={exe_detail}, "
            f"process loopback check failed ({exc}), backend=unknown"
        )
        return

    try:
        supported = bool(capture_proc.is_supported())
    except Exception as exc:  # noqa: BLE001
        print(
            f"[doctor] capture: mode={mode}, player={player_detail}, exe={exe_detail}, "
            f"process loopback check failed ({exc}), backend=unknown"
        )
        return

    pid: int | None = None
    if not exe:
        pid_detail = "no player executable"
    else:
        pid_detail = "no audio session"
        if supported:
            try:
                pid = capture_proc.resolve_player_pid(exe)
            except Exception as exc:  # noqa: BLE001
                pid_detail = f"session check failed ({exc})"
            if pid is not None:
                pid_detail = f"pid={pid}"

    try:
        from mvsync.capture import select_backend
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        print(
            f"[doctor] capture: mode={mode}, player={player_detail}, exe={exe_detail}, "
            f"backend check failed ({exc}), backend=unknown"
        )
        return
    decision = select_backend(mode, supported, pid)
    backend = "process loopback" if decision == "process" else "device loopback"
    print(
        f"[doctor] capture: mode={mode}, player={player_detail}, exe={exe_detail}, "
        f"process loopback supported={supported}, {pid_detail}, backend={backend}"
    )


def doctor() -> int:
    from mvsync import devlog

    log_path = devlog.configure()
    if log_path is not None:
        print(f"[log] writing {log_path}")
    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        suffix = f" ({detail})" if detail else ""
        print(f"[doctor] {name}: {'ok' if ok else 'FAIL'}{suffix}")
        devlog.log("doctor", name=name, ok=ok, detail=detail)
        if not ok:
            failures += 1

    check("python >= 3.12", sys.version_info >= (3, 12), sys.version.split()[0])

    for tool in ("yt-dlp", "mpv", "ffmpeg", "ffprobe"):
        found = paths.find_tool(tool)
        if found is not None:
            check(tool, True, str(found))
        elif tool == "yt-dlp":
            check(tool, False, "missing; install it from https://github.com/yt-dlp/yt-dlp/releases")
        else:
            check(tool, False, "missing; run bootstrap")

    try:
        from mvsync.player_smtc import PlayerBridge

        bridge = PlayerBridge()
        state = bridge.current()
        if state is None:
            check("player session", False, "none found; start playback in your media player")
        else:
            detail = (
                f"{state.artist} - {state.title} @ {state.position_s:.1f}s, "
                f"playing={state.playing}"
            )
            check("player session", True, detail)
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        check("winsdk / player session", False, str(exc))

    try:
        from mvsync.capture import default_loopback

        device = default_loopback()
        check("loopback device", True, getattr(device, "name", "default speaker"))
    except Exception as exc:  # noqa: BLE001
        check("loopback device", False, str(exc))

    _doctor_capture_report()

    try:
        import librosa

        check("librosa", True, librosa.__version__)
    except Exception as exc:  # noqa: BLE001
        check("librosa", False, str(exc))

    print(f"[doctor] {'all checks passed' if not failures else f'{failures} check(s) failed'}")
    return 1 if failures else 0


def _scan(url: str, pick: int | None = None) -> int:
    from mvsync.scan_runtime import run_scan

    return run_scan(url or None, pick=pick)


def _play(url: str, pick: int | None = None) -> int:
    from mvsync.sync_runtime import run_play

    return run_play(url or None, pick=pick)


def _gui() -> int:
    from mvsync.gui import run_gui

    return run_gui()


def _fix() -> int:
    from mvsync.gui import run_fixer

    return run_fixer()


def _parse_menu_args(choice: str) -> tuple[str, int | None]:
    """Split "3 <url or --pick N>" into the optional url and candidate number."""
    parts = choice.split()
    url_parts: list[str] = []
    pick: int | None = None
    index = 1
    while index < len(parts):
        if parts[index] == "--pick" and index + 1 < len(parts) and parts[index + 1].isdigit():
            pick = int(parts[index + 1])
            index += 2
        else:
            url_parts.append(parts[index])
            index += 1
    return " ".join(url_parts), pick


def menu() -> int:
    print("mvsync")
    while True:
        print()
        print(MENU)
        try:
            choice = input("select: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("", "0", "q", "quit"):
            return 0
        if choice == "1":
            doctor()
        elif choice == "4":
            _fix()
        elif choice[:1] in ("2", "3"):
            url, pick = _parse_menu_args(choice)
            try:
                if choice[0] == "2":
                    _scan(url, pick)
                else:
                    _play(url, pick)
            except KeyboardInterrupt:
                print()
        else:
            print("unknown choice")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mvsync",
        description="Watch a muted YouTube music video in mpv, synced to your media player.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("gui", help="open the main window (the default with no arguments)")
    subparsers.add_parser("menu", help="open the old console menu")
    subparsers.add_parser("doctor", help="check environment, devices, and player session")
    scan = subparsers.add_parser("scan", help="scan the current track (auto-finds its MV)")
    scan.add_argument("url", nargs="?", help="youtube url; omit to find the MV automatically")
    scan.add_argument(
        "--pick", type=int, metavar="N", help="use candidate N from the printed search list"
    )
    play = subparsers.add_parser("play", help="play a music video synced to the player")
    play.add_argument("url", nargs="?", help="youtube url; omit to reuse the stored alignment")
    play.add_argument(
        "--pick",
        type=int,
        metavar="N",
        help="use candidate N for an unscanned track's MV",
    )
    subparsers.add_parser("fix", help="pick the right MV for the current track (opens a window)")
    return parser


def _safe_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        if not args_list:
            return _gui()
        parser = _build_parser()
        args = parser.parse_args(args_list)
        if args.command == "gui":
            return _gui()
        if args.command == "menu":
            return menu()
        if args.command == "doctor":
            return doctor()
        if args.command == "scan":
            return _scan(args.url or "", args.pick)
        if args.command == "play":
            return _play(args.url or "", args.pick)
        if args.command == "fix":
            return _fix()
    except KeyboardInterrupt:
        print()
        return 130
    parser.print_help()
    return 2
