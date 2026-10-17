# AGENTS.md

## What this is

`mvsync`: a Windows-only CLI that plays a muted YouTube MV in mpv while the
lossless Spotify desktop track stays the master clock. Spotify is observed and
controlled through the Windows SMTC media session only: no Spotify API, no
cookies, no account credentials in this repo. `scan` records one listen of the
current track and builds a per-track alignment map (anchors plus skip spans);
`play` follows playlists, reusing maps and background-recording first listens.

## Commands (from the repo root)

```
run.bat                     bootstrap (venv, tools) then interactive menu
run.bat --check             bootstrap status only
run.bat play|scan|doctor    normal entrypoints
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

- Windows-only by design (winsdk, soundcard loopback, msvcrt, named-pipe mpv
  IPC). Do not add cross-platform shims.
- Pure logic, unit-testable with synthetic data: `clock`, `align`, `match`,
  `timeline`, `align_store`, `sync_planner`, `mv_search` scoring.
- IO adapters and runtimes: `spotify_smtc`, `capture`, `mpv_ctl`, `mv_audio`,
  `sync_runtime`, `scan_runtime`, `radio`, `session`, `paths`.

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
- Every path goes through `paths.py` (relative to the project root; the folder
  must stay movable). `data/` is generated: deleting `cache` is safe, deleting
  `alignments` loses sync maps.
- Console output is English, prefixed `[scan]`, `[play]`, `[search]`,
  `[doctor]`.
- One session at a time is enforced by `data/session.lock` (pid-based, stale
  locks are stolen). `play` and `scan` both claim it.

## Workflow

- Implement with TDD: write the failing test first, then the code. The pure
  logic split above exists so this works without hardware. Load the
  `test-driven-development` skill for anything non-trivial.
- Before calling anything done, run the full pytest command and ruff from
  Commands above. Line length is 100. Match the house style: type hints,
  short docstrings over comments, no decorative output.
- Keep this file current: when commands, architecture, or invariants change,
  update the affected lines here in the same change.
