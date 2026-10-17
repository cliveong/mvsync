"""Command-line entry point: doctor, scan, play, interactive menu.

Heavy imports (winsdk, soundcard, librosa, the runtimes) happen lazily so the
menu and ``--help`` stay fast.
"""

from __future__ import annotations

import argparse
import contextlib
import sys

from mvsync import paths

MENU = """
1) doctor            check environment, devices, and the Spotify session
2) scan [url] [--pick N]   scan the current track (auto-finds its MV)
3) play [url] [--pick N]   play synced to Spotify (new tracks sync next time)
0) quit
""".strip()


def doctor() -> int:
    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        suffix = f" ({detail})" if detail else ""
        print(f"[doctor] {name}: {'ok' if ok else 'FAIL'}{suffix}")
        if not ok:
            failures += 1

    check("python >= 3.12", sys.version_info >= (3, 12), sys.version.split()[0])

    for tool in ("yt-dlp", "mpv", "ffmpeg", "ffprobe"):
        found = paths.find_tool(tool)
        check(tool, found is not None, str(found) if found else "missing; run bootstrap")

    try:
        from mvsync.spotify_smtc import SpotifyBridge

        bridge = SpotifyBridge()
        state = bridge.current()
        if state is None:
            check("spotify session", False, "none found; open the Spotify desktop app")
        else:
            detail = (
                f"{state.artist} - {state.title} @ {state.position_s:.1f}s, "
                f"playing={state.playing}"
            )
            check("spotify session", True, detail)
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        check("winsdk / spotify session", False, str(exc))

    try:
        from mvsync.capture import default_loopback

        device = default_loopback()
        check("loopback device", True, getattr(device, "name", "default speaker"))
    except Exception as exc:  # noqa: BLE001
        check("loopback device", False, str(exc))

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
        description="Watch a muted YouTube music video in mpv, synced to lossless Spotify.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="check environment, devices, and Spotify session")
    scan = subparsers.add_parser("scan", help="scan the current Spotify track (auto-finds its MV)")
    scan.add_argument("url", nargs="?", help="youtube url; omit to find the MV automatically")
    scan.add_argument(
        "--pick", type=int, metavar="N", help="use candidate N from the printed search list"
    )
    play = subparsers.add_parser("play", help="play a music video synced to Spotify")
    play.add_argument("url", nargs="?", help="youtube url; omit to reuse the stored alignment")
    play.add_argument(
        "--pick",
        type=int,
        metavar="N",
        help="use candidate N for an unscanned track's MV",
    )
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
    if not args_list:
        return menu()

    parser = _build_parser()
    args = parser.parse_args(args_list)
    try:
        if args.command == "doctor":
            return doctor()
        if args.command == "scan":
            return _scan(args.url or "", args.pick)
        if args.command == "play":
            return _play(args.url or "", args.pick)
    except KeyboardInterrupt:
        print()
        return 130
    parser.print_help()
    return 2
