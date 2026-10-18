# Public Release Design

## Intent

Publish `mvsync` as a player-agnostic sync tool, not a Spotify add-on and not a
downloader. The repository should look and behave like a small utility that
plays a muted music video beside the track the user is already playing in their
own media player. The user's own accounts and their own media player remain the
source of audio.

## Goals

- A public `public-release` branch whose tracked tree contains only `src/`,
  `tests/`, `bootstrap.py`, `run.bat`, `setup.bat`, the requirement files,
  `pyproject.toml`, `.gitignore`, and a two-section `README.md`.
- The public branch is isolated. `main` stays the maintainer's local
  Spotify-specific build, including its bootstrap that downloads `yt-dlp`.
  Nothing from `public-release` is merged back into `main`.
- No tracked `tools/`, `data/`, `.venv/`, `AGENTS.md`, plans, spikes, or
  `requirements.lock`.
- Any Windows media player visible in the system media controls can be selected
  from the main window.
- Per-process capture keeps working for players whose executable can be matched,
  with a manual override and a whole-device fallback.
- `setup.bat` installs Python dependencies and downloads mpv and ffmpeg. It
  never downloads or bundles `yt-dlp`; the user installs that themselves.
- Logging stays off by default. No log file is written unless `dev_log` is on.

## Non-goals

- No local-video file mode in this release (that was option 2, not chosen).
- No renaming of the alignment JSON field `spotify_s`. Existing maps stay
  readable; the public surface and user text stop saying Spotify.
- No Spotify API, cookies, or credentials.
- No music content, DRM bypass, or redistribution.

## Decisions

- All public-release changes live on `public-release` only. `main` gets the
  current local fixes in logical commits, then stays as the local
  Spotify-specific build.
- Player selection is a read-only GUI dropdown in the main window plus the
  existing `data/config.json` for advanced users. An empty `player` key means
  auto-select: the first playing media session, otherwise the first session.
  On the public branch, Spotify also works with no config file; the maintainer
  keeps using the untouched `main` build.
- The public tree drops `AGENTS.md`, `docs/superpowers/` (plans and this
  design), `spikes/`, and the tracked `requirements.lock`. `requirements.lock`
  stays gitignored because bootstrap regenerates it.
- Capture auto-detects the player executable from the selected SMTC app id,
  with `player_exe` as a manual override. If no executable matches, capture
  falls back to the whole output device.
- `yt-dlp` is user-installed. Setup checks for it and prints the official
  release link; search and fetch errors repeat that link.
- `setup.bat` downloads portable ffmpeg when it is missing. The current
  bootstrap only prints a winget hint, which is too much friction for a public
  first run.
- `capture_mode` values become `"process"` (default) and `"device"`;
  `"spotify"` remains an accepted alias for `"process"`.
- The public README has exactly two second-level sections: `Install` and
  `How to use`.

## Player model

`player_smtc.PlayerBridge` wraps the Windows media session manager. It exposes:

- `sessions() -> list[PlayerSession]` for the GUI dropdown.
- `current() -> PlayerState | None` for the runtimes.
- `toggle_play_pause()` and `seek(seconds)` for scanning.
- `PlayerBridge(match=None)`; `None` reads `config.load_player()`.

Selection rule: a non-empty `match` selects the first app id containing that
substring (case-insensitive). With no match, the first playing session wins,
then the first session. No session means `None`, never an exception.

`PlayerSession(app_id, title, artist, playing)` is the only type the GUI needs.
`PlayerState` replaces `SpotifyState`; its fields are unchanged.

## Capture model

`player_process.guess_player_exe(app_id)` scans running process names and
returns the first executable whose stem appears in the app id
(`spotify.exe` for a Spotify app id, `vlc.exe` for VLC). It is a heuristic and
is always overridable with `player_exe`.

`capture_proc.resolve_player_pid(exe_name)` enumerates render audio sessions
and returns the pid whose process name matches `exe_name`, or `None`.

`capture.make_recorder` resolves the exe in this order: `player_exe`, then
`guess_player_exe(player)`. No exe means the whole-device recorder, with one
console line explaining why. A failed pid lookup also falls back. Process
capture still requires Windows build 20348 or newer.

## Setup and media tools

`bootstrap.py` continues to create the venv and install dependencies, and it
downloads mpv and portable ffmpeg into `tools/`. It no longer downloads
`yt-dlp`. If `yt-dlp` is missing it prints:
`[bootstrap] yt-dlp is missing. Install it with: winget install yt-dlp.yt-dlp, or download it from https://github.com/yt-dlp/yt-dlp/releases`

`setup.bat` runs the bootstrap and pauses. `run.bat` keeps auto-bootstrapping,
so a first run without setup still installs the Python side, then reports the
missing `yt-dlp` link.

## Positioning

The README says plainly:

- `mvsync` does not provide music, bypass DRM, or redistribute anything.
- The audio comes from the player the user already has open.
- `yt-dlp` is not bundled or downloaded by setup.
- Captured audio is local, used only to align the video, and deleted after a
  successful fit.
- Users must follow the terms of the services they use.

This keeps the project honest. It does not make YouTube or Spotify terms
compliance automatic, and the README does not claim that it does.

## Left to the builder

Exact widget spacing, the wording of the one-line fallback messages, and the
choice of a license are left open. The plan fixes the interfaces, tests, file
names, and user-visible behavior.
