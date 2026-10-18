# mvsync

Watch a muted YouTube music video in mpv while the lossless Spotify track stays
the master clock. A one-time scan per track builds a timeline map (including
skipping the non-music parts of the MV: intros, story breaks, outros), then
every play follows it with rate nudges and hard seeks.

It handles playlists too: scanned tracks swap in their own synced video
automatically, and a new track plays its MV roughly while being recorded in the
background, so from the second listen onward it is fully synced.

Spotify playback is never modified: the desktop client is observed and
controlled through the Windows SMTC media session (no Spotify API, no cookies,
no account credentials anywhere in this project).

## Requirements

- Windows 10 or 11 with the Spotify desktop app (the audio source).
- Spotify Premium (ad-free) is assumed: an ad would be treated as an unknown track.
- Python 3.12 available through the `py` launcher.
- ffmpeg on `PATH` or in `tools/ffmpeg/bin` (used for audio decoding).
- The video plays muted in mpv; all audio comes from Spotify.

## Quick start

Double-click `run.bat`. On first launch it:

1. Creates `.venv` (Python 3.12 via the `py` launcher) and installs pinned deps.
2. Downloads portable `yt-dlp.exe` and `mpv` into `tools/` when missing.
3. Writes a project-local mpv config so your global mpv setup is never used.
4. Opens the main window: Play, Scan, Fix MV, Doctor, and Quit, with a live log
   of everything the session prints.

Everything is idempotent: run it as often as you like, move the folder anywhere,
re-run and it self-heals.

## Everyday use

Double-click `run.bat`. The main window opens. Start the song (or playlist) in
Spotify, press `Refresh track` if the title at the top is stale, then press
`Play`. Pressing `Play` again stops the session; `Quit` closes the window. The
other buttons grey out while something is running, and all four grey out when
another session already holds the lock.

That is it. Pressing `Play` looks up the track that is currently playing:

- Already scanned: the saved map is reused and the video starts immediately.
- Never scanned: the video starts right away with the found MV, roughly synced,
  while the track is recorded in the background. The alignment is computed right
  after the song ends, so the second listen is fully synced. Want it perfect on
  the very first listen? Press `Scan` for it first.
- Started while Spotify is paused: everything waits with you. The video loads
  paused, and a first-listen recording starts the moment you press play.
- Playlist running: when Spotify moves to the next track, the video switches
  with it. A scanned track swaps to its own synced video. A new track plays its
  found MV roughly in sync (no interruption to your listening) while the
  loopback records it; the alignment is computed right after the song ends, so
  the next time you hear that track it plays fully synced.

During playback the mpv video window holds the keyboard focus, so the key
bindings do not fire while it has focus. Use the small `q` `r` `[` `]` `p`
buttons in the main window instead, or click back into the main window and press
the key there. The follow loop reads the keys about every 250 ms, and only once
it is running, so they do nothing during the video download that can take up to
90 seconds at the start of a track.

| Key | Action |
| --- | --- |
| `q` | quit the video (Spotify keeps playing) |
| `r` | resync the video immediately |
| `[` `]` | nudge the offset by 50 ms (saved for next time) |
| `p` | play/pause Spotify |

If the auto-picked MV is the wrong one (for example a remix instead of the
original), you have five ways to force the right one:

- Paste a URL (or a bare video id) into the window's URL entry and press
  `Use this URL`; that scans the current track against exactly that MV.
- At the ready prompt during a console scan, type the candidate number from the
  printed list (or paste a URL, or a bare video id) instead of pressing Enter.
- `run.bat scan --pick 3` uses candidate 3 from the numbered list printed
  during the search.
- `run.bat scan <url>` uses exactly the URL you give.
- The `Fix MV` button (or `run.bat fix`) opens a picker window for the current
  track, as described under "Picking a different MV" below.

Either way the newest scan for that track wins: `play` and playlist
transitions reuse it automatically. `play --pick N` picks candidate N for the
MV used on an unscanned track's first listen (the same list is printed), and
the console menu accepts it too: at the `select:` prompt type for example
`2 --pick 3`.

### Picking a different MV

The `Fix MV` button (or `run.bat fix`) opens a window listing the ranked
candidates for the current track, with the one currently in use marked.
Double-click a row to preview it in the browser, then press `Use this MV`:
Spotify rewinds and plays the track once while the new MV is recorded, and
the map is saved for it (replacing the others for that track). The automatic
search favors the track artist's own video, so wrong picks get rarer and
this window fixes what still slips through. It opens in a separate process,
because Tk cannot open a second window from another thread, so the log pane
keeps updating while the picker is up. The other buttons stay greyed out until
it closes.

### Pausing and skipping

During a play session:

- Pausing Spotify pauses the video with it and resumes in sync. A mid-track
  pause never ends the session; pausing at the very end of the last queued
  song exits after about five seconds.
- Pausing a never-scanned song for more than two seconds voids that
  first-listen recording (it would produce a bad map). The video still pauses
  and resumes with you; the track simply gets another chance on its next
  uninterrupted listen.
- Skipping to another track (next, previous, or any jump) is handled like a
  natural track change: the video swaps immediately to the new track, synced
  if it is known, rough plus a new recording if it is not. Skipping away from
  a song that was being first-listen recorded discards that partial recording.
- Restarting the current song (a seek) is fine on a scanned track, the video
  hard-seeks back to match. On a first listen the video follows your seek
  roughly (it jumps by the same delta) while that song's recording is
  discarded, same as a pause.

During a scan, any pause, seek, or skip aborts the scan cleanly and nothing
is saved.

### Scanning a track explicitly

The `Scan` button (or `run.bat scan`) records the current track once and saves
its alignment, without playing a video afterwards. Useful to make a track's very
first listen already synced. With the URL entry empty the MV is found
automatically; fill the entry (or paste into it) to scan against exactly that
video instead. A console scan also asks at the ready prompt, where you can type
the number of a different candidate (or paste a URL, or a bare video id)
instead of pressing Enter.

While a scan records, leave Spotify alone: pausing, seeking, skipping, or a
track change aborts it cleanly (nothing is saved). The log shows the chosen MV
candidates, any skipped MV sections, and the final save.

### Doctor

The `Doctor` button (or `run.bat doctor`) checks Python, the portable tools, the
Spotify session, the loopback device, and librosa. Run it whenever something
looks off.

## Command line

The window is the everyday interface; these are the equivalents for scripts and
for when you want the output in a console. `run.bat` with no arguments opens the
window, and `run.bat gui` asks for it explicitly.

```
run.bat                 the main window (same as run.bat gui)
run.bat gui             open the main window explicitly
run.bat menu            the old console menu (1 doctor, 2 scan, 3 play, 4 fix, 0 quit)
run.bat doctor          environment and device report
run.bat scan [url]      scan the current track (auto-finds its MV)
run.bat play [url]      play synced to Spotify (new tracks sync from the 2nd listen)
run.bat fix             pick the right MV for the current track (opens a window)
run.bat scan --pick N   scan with candidate N from the printed search list
run.bat play --pick N   use candidate N for an unscanned track's MV
run.bat --check         bootstrap status without changing anything
```

## What lands on disk

Everything stays inside the project folder under `data/`, so the whole thing is
portable:

```
data/cache/        downloaded MV video + audio per song (the bulk, up to ~100 MB per song at 4K)
data/recordings/   loopback recordings used by scans and first listens (deleted once their map is saved)
data/alignments/   the per-track sync maps (small JSON, the valuable part)
data/session.lock  single-session marker while play, scan, or fix runs (stolen when stale)
```

The window only reads `session.lock`: it greys out its buttons when another live
session holds it, and never writes or steals the file itself.

Deleting `cache` is safe, it is re-downloaded on demand. Deleting `alignments`
loses the sync maps, so those tracks need a rescan (or a first listen) again.

A scan or first-listen recording is automatically discarded when it would
produce a bad map: interrupted playback, silence, a capture hiccup that lost
audio, or a recording that stopped early. A recording whose fit succeeded is
deleted together with the map; only a fit that found no matching candidate
keeps its wav file for a manual refit. During the background fit the
alignment is also checked against the
picked MV, and a wrong pick (a live clip, a different song) is rejected and the
next search candidate is tried automatically. An MV that re-edits the song's
arrangement still fits: the map is exact wherever the MV carries the music and
approximate through its cuts.

First-listen fits run in the background. If you quit the session while a fit is
still running, it waits for that fit before exiting so the map is not lost.

## Notes and limitations

- Only the Spotify desktop app works, and it must play through the default
  output device (the loopback records that device). Switching playback to
  another device with Spotify Connect, or changing the default output device
  mid-song, voids a first listen in progress (the map is just learned again
  next time).
- The loopback records everything the default output device plays, so keep
  notifications, games, and other apps quiet during a first listen or scan. A
  contaminated recording is rejected rather than saved as a bad map.
- Run one session at a time: `play`, `scan`, and `fix` claim `data/session.lock`,
  so a second `run.bat` refuses to start while one is live (a stale lock from a
  crash is stolen automatically), and the window's buttons stay greyed out.
- The video is muted by design; audio always comes from Spotify.
- YouTube sometimes blocks direct streaming for mpv. That is why videos are
  downloaded to `data/cache` before playback, which also keeps seeking instant.
- If no MV is found for a track (or the download fails), the previous video
  freezes until the next track; Spotify keeps playing.
- If the MV is shorter than the track, the video holds its last frame for the
  rest of the song instead of ending early.
- A remix of the right song cannot be told apart from the original by audio
  matching alone; the ranking (title, channel, length) guards that. Force the
  right video with a URL if needed.
- Do not drag URLs into the mpv window while a session is running.
- The first listen of a new song is roughly synced (MV intros are unknown until
  the fit finishes), and the video window may be black for ten seconds or so
  while the video downloads.
- The hotkeys only reach the play loop once it is running, so pressing `q` or
  the on-screen `q` button during that first download does nothing; wait for the
  video to appear, then stop it.

## Development

```
run.bat --check            # bootstrap status without changing anything
.venv\Scripts\python -m pytest
.venv\Scripts\python -m pytest -m "not dsp"     # fast subset
.venv\Scripts\python -m ruff check src tests spikes bootstrap.py
set PYTHONPATH=src & .venv\Scripts\python -m mvsync doctor
set PYTHONPATH=src & .venv\Scripts\python -m mvsync gui
```

Spikes run the same way, e.g. `set PYTHONPATH=src & .venv\Scripts\python spikes\spike_smtc.py`.
`set MVSYNC_SCAN_DEBUG=1` prints a per-poll trace during scans.

## Layout

```
src/mvsync/    Python package (pure logic separated from IO adapters)
tests/         pytest suite for the pure modules (DSP tests marked `dsp`)
spikes/        M0 measurement scripts (SMTC cadence, mpv IPC, loopback, PCM)
tools/         portable mpv, yt-dlp, and the project-local mpv config
data/          generated: cache, recordings, alignments (see above)
```

## Status

- M0 measurement spikes
- M1 playback alignment scan (with candidate fitting, wrong picks self-heal)
- M2 live sync loop (follows playlists, first-listen recording + background fit)
- M3 polish: watcher mode and packaging are still open
