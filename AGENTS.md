# AGENTS.md

## What this is

`mvsync`: a Windows-only app that plays a muted YouTube MV in mpv while the
lossless Spotify desktop track stays the master clock. Spotify is observed and
controlled through the Windows SMTC media session only: no Spotify API, no
cookies, no account credentials in this repo. `scan` records one listen of the
current track and builds a per-track alignment map (anchors plus skip spans);
`play` follows playlists, reusing maps and background-recording first listens.
`run.bat` with no arguments opens the Tk main window (`gui`), which drives the
same runtimes; the subcommands and the old console `menu` still work.

## Commands (from the repo root)

```
run.bat                         bootstrap (venv, tools) then the main window
run.bat --check                 bootstrap status only
run.bat gui|menu                main window, or the old console menu
run.bat play|scan|fix|doctor    normal entrypoints (unchanged by the GUI)
.venv\Scripts\python.exe -m pytest                          # full suite
.venv\Scripts\python.exe -m pytest tests/test_fit.py::test_session_claim_refuses_live_lock
.venv\Scripts\python.exe -m pytest -m "not dsp"             # fast subset
.venv\Scripts\python.exe -m ruff check src tests spikes bootstrap.py
```

`pythonpath=src` and the `dsp`/`integration` markers live in `pyproject.toml`.
Tests must stay runnable without network, real audio devices, SMTC, or mpv;
anything needing them gets a marker. Portable tools land in `tools/`; ffmpeg
resolves from `PATH` or `tools/ffmpeg/bin`.

## Architecture

- Windows-only by design (winsdk, soundcard loopback, comtypes/pycaw process
  loopback, msvcrt, named-pipe mpv IPC). Do not add cross-platform shims.
- Pure logic, unit-testable with synthetic data: `clock`, `align`, `match`,
  `timeline`, `align_store`, `sync_planner`, `mv_search` scoring.
- IO adapters and runtimes: `spotify_smtc`, `capture`, `capture_proc`, `config`,
  `mpv_ctl`, `mv_audio`, `sync_runtime`, `scan_runtime`, `radio`, `session`,
  `paths`, `gui`, `gui_console`.

## Invariants (each one cost a debugging session to learn)

- Alignment JSON `offset_s` is `-(track position where the map's spotify axis
  is zero)`; the planner queries at `spotify_s + offset_s` (the offset is added
  to each query, so it is negative for maps made mid-track). `scan` saves
  `offset_s=-position_at_play`; the radio fit keeps the recording's own
  timeline (no pre-roll cut) and saves `offset_s=-position_at_record_start`.
- `librosa.sequence.dtw(C=..., subseq=True)` flips the returned path when the
  supplied cost matrix is taller than wide (it assumes an internal transpose
  that never happened); `compute_alignment_from_audio` unflips it so path pairs
  stay `(recording, video)`. Without the unflip, any recording longer than the
  MV audio dies in the cost lookup with `IndexError` (regression test:
  `tests/test_align_dsp.py::test_recording_longer_than_video_keeps_orientation`).
- `looks_sane` accepts re-edited MVs via `MIN_SANE_RATE_SHARE`: arrangement
  cuts smear a minority of the covered span into off-rate segments, so a
  majority in rate-1 segments passes. Wrong songs still fail on anchors or
  coverage, which stays strict.
- SMTC positions are coarse (about 0.5 s quantized, refreshed every few
  seconds). Never use raw positions: go through `clock.extrapolate`, and keep
  the `END_EPS_S` near-end tolerance before flagging jumps (the timeline
  resets before the metadata switches at track end).
- Videos are always downloaded into `data/cache` via `fetch_video_mp4` before
  playback: YouTube 403s mpv's direct streaming. A failed download (or a track
  with no MV) must never kill the session: freeze the previous video
  (`_freeze_video`) and keep following, recording the listen when possible.
- When the MV is shorter than the track, it must hold its last frame while the
  music continues: mpv runs with `keep_open=True`, and rough mode keeps the
  video paused on `eof_reached` rather than unpausing a finished video.
- First-listen recordings are deleted once their fit saves a map and after
  every `scan` (success or abort); a failed fit keeps the wav so the listen
  can be refitted by hand. Interrupted listens (pause over 2 s, seek) are
  discarded.
- A first-listen recording is trusted only when wall time and captured audio
  agree (`MAX_CAPTURE_LOSS_S`) and `RadioWatch` saw no pause over 2 s or seek.
- Scans and first listens capture only the Spotify process tree on Windows build
  20348+ (`capture_proc`, backend `spotify`), falling back to the whole-device
  loopback (`capture`, backend `device`) when the OS is too old, the import
  fails, or Spotify has no audio session. `capture.select_backend` is the single
  decision `make_recorder` and doctor both use. `ready_epoch` is stamped on the
  first captured packet, not at activation, or `offset_s` shifts.
- `sys.coinit_flags = 0` must be the first executable statement of
  `capture_proc.py`, before any `comtypes` import: SoundCard may initialize MTA
  and comtypes would otherwise raise `RPC_E_CHANGED_MODE`.
- Every path goes through `paths.py` (relative to the project root; the folder
  must stay movable). The capture mode is an optional hand-written override in
  `data/config.json` (`capture_mode`, `"spotify"` by default or `"device"`); the
  app never writes it. `data/` is generated otherwise: deleting `cache` is safe,
  deleting `alignments` loses sync maps.
- Console output is English, prefixed `[scan]`, `[play]`, `[search]`,
  `[capture]`, `[doctor]`, and `[gui]` for the window's own lines (which the
  GUI's log pane shows beside the captured ones).
- One session at a time is enforced by `data/session.lock` (pid-based, stale
  locks are stolen). `play`, `scan`, and `fix` all claim it. `session.claim` is
  the only writer of that file, and `session.lock_holder` is the strictly
  read-only probe the GUI calls to grey out its buttons: it must never steal the
  lock, and it excludes our own pid so the window does not mistake its own
  worker for an external session. `session.is_locked` is only its boolean
  wrapper, for callers that do not need the pid.
- The play loop takes its hotkeys from an injected callable (`run_play(keys=...)`,
  `KeySource.poll` from the window, `_read_key` from the console) and only reads
  them once `_follow` is running, so keys pushed while a video is still
  downloading are never seen.
- Long GUI operations run off the Tk main thread (`MainWindow._run` starts a
  worker and `redirect_stdout` into a `LogSink`, pumped back with `after`).
  `MainWindow` is the single Tk root: a second one created in a worker thread
  hangs, which is why `Fix MV` launches `mvsync fix` as a separate process.
- A track has one authoritative map: `scan`/`fix` saving a map prunes other
  maps with the same track id (`prune_other_alignments`); `play` falls back to
  the newest by mtime (`load_newest_alignment`).

## Workflow

- Implement with TDD: write the failing test first, then the code. The pure
  logic split above exists so this works without hardware. Load the
  `test-driven-development` skill for anything non-trivial.
- Before calling anything done, run the full pytest command and ruff from
  Commands above. Line length is 100. Match the house style: type hints,
  short docstrings over comments, no decorative output.
- Keep this file current: when commands, architecture, or invariants change,
  update the affected lines here in the same change.
