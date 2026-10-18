# Public Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a player-agnostic `public-release` branch whose tracked tree contains only the public files, with a GUI player picker, generic capture, a setup script, a user-installed `yt-dlp`, and a two-section README, while leaving the local `main` build unchanged.

**Architecture:** `main` keeps the Spotify-specific local build. A checkpoint commit preserves the current local fixes, then `public-release` branches from it. On that branch, the Windows media-session adapter becomes `PlayerBridge` with a configurable app-id match and a GUI dropdown; capture resolves a configurable player executable with a whole-device fallback; setup downloads mpv and ffmpeg but only checks for `yt-dlp`; and the public tree drops developer-only files. Nothing from the public branch is merged back into `main`.

**Tech Stack:** Python 3.12, Tkinter, winsdk SMTC, pycaw/comtypes process loopback, soundcard loopback, yt-dlp, mpv, ffmpeg, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-19-public-release-design.md`

## Global Constraints

- The public-release changes happen on `public-release` only. `main` is checkpointed once in Task 0 and then left alone. Do not merge `public-release` back into `main`.
- Windows-only. Python 3.12 or newer.
- The alignment JSON schema keeps `spotify_s` and all existing maps stay readable. Do not rename the schema in this branch.
- The public tree never tracks `tools/`, `data/`, `.venv/`, `AGENTS.md`, `docs/superpowers/`, `spikes/`, or `requirements.lock`.
- The public `bootstrap.py` never downloads or bundles `yt-dlp`. It checks for it and prints the official release link.
- `setup.bat` downloads portable mpv and ffmpeg into `tools/` and installs the Python dependencies.
- The developer log is off by default and only turns on with `dev_log` in `data/config.json` or `MVSYNC_DEV_LOG=1`.
- Config writes preserve unrelated keys (`capture_mode`, `player`, `player_exe`, `dev_log`) and stay atomic.
- `capture_mode` accepts `"process"` and `"device"`; `"spotify"` is a legacy alias for `"process"`.
- Tests stay runnable without network, hardware, SMTC, or mpv. Anything requiring them keeps its existing marker.
- Line length 100, ruff clean, full pytest green.

## Review Focus

- Two media sessions are open (a browser and a player): the configured match wins, otherwise the playing session, never a paused browser tab. Task 2 pins this.
- A selected player has no matching audio session or no guessed executable: whole-device capture with one clear line, never a crash. Task 3 pins this.
- Picking a player must not erase `capture_mode` or `dev_log` from `data/config.json`. Task 1 pins the merge.
- `yt-dlp` is missing: setup prints the official link, the app still starts, and search/fetch errors repeat the link. Task 5 pins the messages and status.
- The ffmpeg zip is missing or malformed: setup keeps the tools folder usable and prints the manual fallback. Task 5 pins extraction.
- `README.md` has exactly two second-level sections. Task 6 checks it.

---

### Task 0: Commit the local work in logical commits, then create the public branch

**Files:**
- Git only. No source edits in this task.

**Interfaces:**
- Consumes: the current uncommitted local work (track transition prep, dev log, MV picker Back button, mpv window persistence) and this plan/design.
- Produces: `main` with the local work committed in logical commits, and the `public-release` branch checked out.

- [ ] **Step 1: Confirm the starting point**

Run:

```powershell
git branch --show-current
git status --short
```

Expected: `main`, with the known modified and untracked files from the local work.

- [ ] **Step 2: Commit the dev log first**

```powershell
git add src/mvsync/devlog.py tests/test_devlog.py src/mvsync/config.py src/mvsync/paths.py src/mvsync/capture.py src/mvsync/cli.py src/mvsync/mv_audio.py src/mvsync/mv_search.py src/mvsync/scan_runtime.py src/mvsync/session.py
git commit -m "Add an opt-in developer log"
```

- [ ] **Step 3: Commit the mpv window behavior next**

```powershell
git add src/mvsync/mpv_ctl.py tests/test_mpv_ctl.py
git commit -m "Keep the mpv window size and fullscreen across video swaps"
```

- [ ] **Step 4: Commit the track transition fix**

```powershell
git add src/mvsync/sync_runtime.py src/mvsync/radio.py tests/test_sync_runtime.py tests/test_fit.py
git commit -m "Keep the old video frozen while the next MV is prepared"
```

- [ ] **Step 5: Commit the MV picker Back button**

```powershell
git add src/mvsync/gui.py tests/test_gui.py
git commit -m "Add a Back button to the MV picker"
```

- [ ] **Step 6: Commit the docs and the release plan**

```powershell
git add README.md AGENTS.md
git commit -m "Document the transition, log, and window behavior"
git add docs/superpowers/specs/2026-10-19-public-release-design.md docs/superpowers/plans/2026-10-19-public-release.md
git commit -m "Plan the public release branch"
```

- [ ] **Step 7: Commit the dependency lock**

```powershell
git add requirements.lock
git commit -m "Lock the process-capture dependencies"
git status --short
```

Expected: a clean working tree on `main`.

- [ ] **Step 8: Create and switch to the public branch**

```powershell
git switch -c public-release
git branch --show-current
```

Expected: `public-release`, created from the committed local work.

- [ ] **Step 9: Do not touch main again**

Leave `main` as the local Spotify-specific build. All later commits stay on `public-release`. Do not merge or rebase `public-release` into `main`.

---

### Task 1: Player-neutral config

**Files:**
- Modify: `src/mvsync/config.py`
- Create: `tests/test_config.py`
- Modify: `tests/test_capture.py` (the existing capture-mode assertions)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `paths.config_path(root)`, the existing `_load` helper.
- Produces:
  - `DEFAULT_CAPTURE_MODE = "process"`, `CAPTURE_MODES = ("process", "device")`.
  - `normalize_capture_mode(mode: str) -> str` (maps `"spotify"` to `"process"`).
  - `load_player(root: Path | None = None) -> str`.
  - `load_player_exe(root: Path | None = None) -> str`.
  - `save_player(app_id: str, exe: str, root: Path | None = None) -> None`.
  - `set_capture_mode` and `_save` preserve unrelated keys.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
import json

from mvsync import config, paths


def write_config(root, payload):
    path = paths.config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_config(root):
    return json.loads(paths.config_path(root).read_text(encoding="utf-8"))


def test_capture_mode_defaults_to_process(tmp_path):
    assert config.load_capture_mode(tmp_path) == "process"


def test_legacy_spotify_mode_maps_to_process(tmp_path):
    write_config(tmp_path, {"capture_mode": "spotify"})
    assert config.load_capture_mode(tmp_path) == "process"


def test_set_capture_mode_preserves_other_keys(tmp_path):
    write_config(tmp_path, {"dev_log": True, "player": "vlc.exe"})
    config.set_capture_mode("device", tmp_path)
    assert read_config(tmp_path) == {
        "capture_mode": "device",
        "dev_log": True,
        "player": "vlc.exe",
    }


def test_save_player_preserves_other_keys(tmp_path):
    config.set_capture_mode("device", tmp_path)
    config.save_player("vlc.exe", "vlc.exe", tmp_path)
    assert config.load_player(tmp_path) == "vlc.exe"
    assert config.load_player_exe(tmp_path) == "vlc.exe"
    assert config.load_capture_mode(tmp_path) == "device"


def test_save_player_without_an_exe_clears_the_old_one(tmp_path):
    config.save_player("vlc.exe", "vlc.exe", tmp_path)
    config.save_player("musicbee.exe", "", tmp_path)
    assert config.load_player_exe(tmp_path) == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_config.py -v
```

Expected: failures for the missing functions and for the default still being `"spotify"`.

- [ ] **Step 3: Implement the config changes**

In `src/mvsync/config.py`:

- Set `DEFAULT_CAPTURE_MODE = "process"` and `CAPTURE_MODES = ("process", "device")`.
- Add `LEGACY_CAPTURE_MODES = {"spotify": "process"}` and
  `normalize_capture_mode(mode: str) -> str`.
- Make `load_capture_mode` normalize the stored value.
- Make `set_capture_mode` normalize, validate, merge into `_load(root)`, and call a new atomic `_save(data, root)`.
- Add `load_player`, `load_player_exe`, and `save_player`. `save_player` sets `player`, sets `player_exe` when non-empty, and removes `player_exe` when empty.
- Keep `load_dev_log` unchanged.

Update the existing capture-mode assertions in `tests/test_capture.py` so the default is `"process"` and the stored `"spotify"` value loads as `"process"`. Keep the unknown-value rejection test.

Use the existing `tempfile.mkstemp` plus `os.replace` pattern already in `set_capture_mode` for `_save`.

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_config.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add src/mvsync/config.py tests/test_config.py
git commit -m "Make the player and capture settings configurable"
```

---

### Task 2: Player-neutral SMTC bridge

**Files:**
- Rename: `src/mvsync/spotify_smtc.py` to `src/mvsync/player_smtc.py`
- Rename: `tests/test_spotify_smtc.py` to `tests/test_player_smtc.py`
- Modify: `src/mvsync/sync_runtime.py`, `src/mvsync/scan_runtime.py`, `src/mvsync/gui.py`, `src/mvsync/cli.py`, `src/mvsync/radio.py`
- Modify: `tests/test_gui.py`, `tests/test_scan_runtime.py`, `tests/test_sync_runtime.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `config.load_player(root)` from Task 1.
- Produces:
  - `player_smtc.PlayerState` (same fields as the old `SpotifyState`).
  - `player_smtc.PlayerSession(app_id, title, artist, playing)`.
  - `PlayerBridge(match: str | None = None)`, `current()`, `sessions()`, `toggle_play_pause()`, `seek(seconds)`.
  - Selection: configured match, else the first playing session, else the first session.

- [ ] **Step 1: Write the failing selection tests**

Append to `tests/test_player_smtc.py`:

```python
class FakeSession:
    def __init__(self, app_id, playing=False):
        self.source_app_user_model_id = app_id
        self._playing = playing

    def get_playback_info(self):
        return self


def test_choose_prefers_a_playing_session(monkeypatch):
    from mvsync import player_smtc

    monkeypatch.setattr(player_smtc, "_is_playing", lambda playback: playback._playing)
    sessions = [FakeSession("one.exe", playing=False), FakeSession("two.exe", playing=True)]
    bridge = player_smtc.PlayerBridge.__new__(player_smtc.PlayerBridge)
    bridge._match = ""
    assert bridge._choose(sessions).source_app_user_model_id == "two.exe"


def test_choose_filters_by_the_configured_match(monkeypatch):
    from mvsync import player_smtc

    monkeypatch.setattr(player_smtc, "_is_playing", lambda playback: playback._playing)
    sessions = [FakeSession("browser.exe", playing=True), FakeSession("vlc.exe", playing=False)]
    bridge = player_smtc.PlayerBridge.__new__(player_smtc.PlayerBridge)
    bridge._match = "vlc"
    assert bridge._choose(sessions).source_app_user_model_id == "vlc.exe"


def test_choose_returns_none_when_nothing_matches(monkeypatch):
    from mvsync import player_smtc

    monkeypatch.setattr(player_smtc, "_is_playing", lambda playback: playback._playing)
    bridge = player_smtc.PlayerBridge.__new__(player_smtc.PlayerBridge)
    bridge._match = "missing"
    assert bridge._choose([FakeSession("vlc.exe")]) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_player_smtc.py -v
```

Expected: import failure for `player_smtc` and missing `PlayerSession`/`_choose`.

- [ ] **Step 3: Rename and implement the bridge**

- `git mv src/mvsync/spotify_smtc.py src/mvsync/player_smtc.py`
- `git mv tests/test_spotify_smtc.py tests/test_player_smtc.py`
- Rename `SpotifyBridge` to `PlayerBridge` and `SpotifyState` to `PlayerState`.
- Add `PlayerSession`.
- `__init__(match=None)`: when `match is None`, use `config.load_player()`.
- Add `_sessions()` returning `list(self._manager.get_sessions())`.
- Add `_choose(sessions)`: the match rule above; when no match, first playing, else first, else `None`.
- Keep `_session()` as `return self._choose(self._sessions())` so existing tests that patch `_session` keep working.
- Add `sessions() -> list[PlayerSession]` reading app id, title, artist, and playing for every session.
- Keep `_resolve`, `_seconds`, `_epoch`, `_is_playing`, `current`, `toggle_play_pause`, and `seek` behavior unchanged.

- [ ] **Step 4: Update every call site and user-facing string**

Run `git grep -l "spotify_smtc\|SpotifyBridge\|SpotifyState"` and update each file. Expected files: the runtimes, GUI, CLI, radio type hints, and tests. Replace user-facing `Spotify` with `player` in `cli.py`, `gui.py`, `sync_runtime.py`, and `scan_runtime.py`. Keep `spotify_s` in the alignment schema and pure logic unchanged.

Update the GUI lazy-import probe in `tests/test_gui.py` from `mvsync.spotify_smtc` to `mvsync.player_smtc`.

- [ ] **Step 5: Run the focused and full suites**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_player_smtc.py tests/test_sync_runtime.py tests/test_scan_runtime.py tests/test_gui.py tests/test_cli.py -q
.venv\Scripts\python.exe -m ruff check src tests spikes bootstrap.py
```

Expected: all pass. Fix any import or string mismatch before committing.

- [ ] **Step 6: Commit**

```powershell
git add -A
git commit -m "Observe any media player, not just Spotify"
```

---

### Task 3: Generic per-process capture

**Files:**
- Create: `src/mvsync/player_process.py`
- Create: `tests/test_player_process.py`
- Modify: `src/mvsync/capture_proc.py`, `src/mvsync/capture.py`, `src/mvsync/cli.py`
- Modify: `tests/test_capture.py`, `tests/test_cli.py`, `tests/test_scan_runtime.py`

**Interfaces:**
- Consumes: `config.load_player`, `config.load_player_exe`, `config.normalize_capture_mode` from Task 1.
- Produces:
  - `player_process.guess_player_exe(app_id: str) -> str | None`.
  - `capture_proc.resolve_player_pid(exe_name: str | None) -> int | None`.
  - `capture.make_recorder` resolves `player_exe`, then the guessed exe, then device fallback.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_player_process.py
def test_guess_player_exe_matches_the_process_stem(monkeypatch):
    import psutil

    from mvsync.player_process import guess_player_exe

    class Process:
        def __init__(self, name):
            self.info = {"name": name}

    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [Process("Spotify.exe")])
    assert guess_player_exe("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify") == "spotify.exe"


def test_guess_player_exe_returns_none_for_an_unknown_app(monkeypatch):
    import psutil

    from mvsync.player_process import guess_player_exe

    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [])
    assert guess_player_exe("some.unknown.app") is None


def test_guess_player_exe_ignores_an_empty_app_id():
    from mvsync.player_process import guess_player_exe

    assert guess_player_exe("") is None
```

Append to `tests/test_capture.py`:

```python
def test_make_recorder_falls_back_when_no_player_exe_is_known(tmp_path, capsys, monkeypatch):
    from mvsync import capture

    monkeypatch.setattr("mvsync.capture.config.load_player_exe", lambda root=None: "")
    monkeypatch.setattr("mvsync.capture.config.load_player", lambda root=None: "")
    recorder = capture.make_recorder(tmp_path / "a.wav", mode="process")
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "no player executable" in capsys.readouterr().out


def test_make_recorder_falls_back_when_the_player_has_no_audio_session(
    tmp_path, capsys, monkeypatch
):
    from mvsync import capture, capture_proc

    monkeypatch.setattr("mvsync.capture.config.load_player_exe", lambda root=None: "vlc.exe")
    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_player_pid", lambda exe: None)
    recorder = capture.make_recorder(tmp_path / "a.wav", mode="process")
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "session not found" in capsys.readouterr().out


def test_make_recorder_accepts_the_legacy_spotify_mode(tmp_path, monkeypatch):
    from mvsync import capture, capture_proc

    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_player_pid", lambda exe: 4242)
    monkeypatch.setattr("mvsync.capture.config.load_player_exe", lambda root=None: "spotify.exe")

    recorder = capture.make_recorder(tmp_path / "a.wav", mode="spotify")
    assert isinstance(recorder, capture.ProcessLoopbackRecorder)
    assert recorder._pid == 4242
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_player_process.py tests/test_capture.py -v
```

Expected: missing `player_process`, missing `resolve_player_pid`, and the old Spotify-specific fallback.

- [ ] **Step 3: Implement `guess_player_exe` and `resolve_player_pid`**

`player_process.py`:

```python
def guess_player_exe(app_id: str) -> str | None:
    needle = (app_id or "").strip().lower()
    if not needle:
        return None
    try:
        import psutil

        for process in psutil.process_iter(["name"]):
            name = (process.info.get("name") or "").lower()
            stem = name.removesuffix(".exe")
            if stem and stem in needle:
                return name
    except Exception:
        return None
    return None
```

`capture_proc.resolve_player_pid` is the old `resolve_spotify_pid` with an `exe_name` argument: normalize to lowercase, append `.exe` when missing, return `None` for an empty name, and compare the audio-session process name to that target. Remove `SPOTIFY_PROCESS` and `resolve_spotify_pid`.

- [ ] **Step 4: Wire the recorder, doctor, and messages**

- `capture.make_recorder`: normalize the mode; for process mode resolve `exe = config.load_player_exe(root) or guess_player_exe(config.load_player(root))`. If no exe, print `[capture] no player executable configured; using device loopback` and return `LoopbackRecorder`. Otherwise resolve the pid and use `select_backend`.
- Change `select_backend`'s process condition from `mode == "spotify"` to `mode == "process"`.
- Update the `tests/test_capture.py` select-backend parametrization to use `"process"`, and keep the legacy `"spotify"` case on `make_recorder` rather than `select_backend`.
- `cli._doctor_capture_report`: print the selected player app id, exe, pid, and backend. Use the same exe resolution.
- Update all `resolve_spotify_pid` references and the old "Spotify audio session" messages in `capture.py`, `cli.py`, and tests.
- Change `tests/test_scan_runtime.py` references to the new names and default mode.

- [ ] **Step 5: Run the focused and full suites**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_player_process.py tests/test_capture.py tests/test_cli.py tests/test_scan_runtime.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests spikes bootstrap.py
```

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add -A
git commit -m "Capture the selected player's process when it can be matched"
```

---

### Task 4: GUI player dropdown

**Files:**
- Modify: `src/mvsync/gui.py`
- Modify: `tests/test_gui.py`

**Interfaces:**
- Consumes: `PlayerBridge.sessions()`, `PlayerSession`, `config.save_player`, `player_process.guess_player_exe`.
- Produces:
  - `player_label(session: PlayerSession) -> str`.
  - `player_index(sessions: list[PlayerSession], match: str) -> int | None`.
  - `MainWindow.refresh_players()` and `MainWindow._select_player()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_player_label_names_the_track_and_app():
    from mvsync.gui import player_label
    from mvsync.player_smtc import PlayerSession

    label = player_label(PlayerSession("vlc.exe", "Song", "Artist", True))
    assert "Song" in label and "Artist" in label and "vlc.exe" in label


def test_player_index_prefers_the_configured_match():
    from mvsync.gui import player_index
    from mvsync.player_smtc import PlayerSession

    sessions = [
        PlayerSession("browser.exe", "Video", "Channel", True),
        PlayerSession("vlc.exe", "Song", "Artist", False),
    ]
    assert player_index(sessions, "vlc") == 1
    assert player_index(sessions, "missing") == 0
    assert player_index([], "vlc") is None


def test_selecting_a_player_saves_the_app_and_guessed_exe(monkeypatch):
    from mvsync import gui
    from mvsync.player_smtc import PlayerSession

    saved = []
    window = StandInWindow()
    window._root_dir = None
    window._players = [PlayerSession("vlc.exe", "Song", "Artist", True)]
    window._player_box = type("Box", (), {"current": lambda self: 0})()
    window._log = lambda message: None
    window.refresh_track = lambda: None
    monkeypatch.setattr(gui, "guess_player_exe", lambda app_id: "vlc.exe")
    monkeypatch.setattr(gui.config, "save_player", lambda app, exe, root=None: saved.append((app, exe)))

    gui.MainWindow._select_player(window)

    assert saved == [("vlc.exe", "vlc.exe")]
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_gui.py -v
```

Expected: missing `player_label`, `player_index`, and `_select_player`.

- [ ] **Step 3: Implement the player row**

- Add the two pure helpers.
- In `_build_ui`, add a row with a `Player` label, a read-only `ttk.Combobox` bound to `self._player_var`, and a `Refresh players` button calling `self.refresh_players`.
- `refresh_players()` calls `PlayerBridge().sessions()`, stores them in `self._players`, fills the combobox with `player_label`, and selects `player_index(sessions, config.load_player(self._root_dir))`.
- `_select_player()` reads the index, gets the app id, guesses the exe with `guess_player_exe`, calls `config.save_player(app_id, exe, self._root_dir)`, logs the choice, and calls `refresh_track`.
- Call `refresh_players()` once after the UI is built.
- Replace `SpotifyBridge`/`SpotifyState` with `PlayerBridge`/`PlayerState` in the window.
- Import `guess_player_exe` at module level from `player_process` (it imports psutil lazily).

- [ ] **Step 4: Run the tests and the full suite**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_gui.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests spikes bootstrap.py
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add src/mvsync/gui.py tests/test_gui.py
git commit -m "Let the user pick their media player in the window"
```

---

### Task 5: Setup script, portable ffmpeg, user-installed yt-dlp

**Files:**
- Create: `setup.bat`
- Modify: `bootstrap.py`, `tests/test_bootstrap.py`
- Modify: `src/mvsync/mv_search.py`, `src/mvsync/mv_audio.py`, `src/mvsync/cli.py`

**Interfaces:**
- Consumes: the existing `download`, `paths.find_tool`, `ensure_mpv_config`.
- Produces:
  - `bootstrap.extract_ffmpeg(archive: Path, target_dir: Path) -> Path | None`.
  - `bootstrap.ensure_ffmpeg` downloads `FFMPEG_URL` and calls `extract_ffmpeg`.
  - `bootstrap.main` checks for `yt-dlp` instead of downloading it.
  - `setup.bat` runs the bootstrap and never launches the app.

- [ ] **Step 1: Write the failing ffmpeg extraction tests**

```python
# tests/test_bootstrap.py
import zipfile


def test_extract_ffmpeg_copies_both_binaries(tmp_path):
    import bootstrap

    archive = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("ffmpeg-x/bin/ffmpeg.exe", b"ffmpeg")
        zipped.writestr("ffmpeg-x/bin/ffprobe.exe", b"ffprobe")

    target = tmp_path / "tools" / "ffmpeg" / "bin"
    result = bootstrap.extract_ffmpeg(archive, target)

    assert result == target / "ffmpeg.exe"
    assert (target / "ffprobe.exe").is_file()


def test_extract_ffmpeg_returns_none_without_the_binaries(tmp_path):
    import bootstrap

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("readme.txt", b"no binaries")

    assert bootstrap.extract_ffmpeg(archive, tmp_path / "bin") is None


def test_the_ytdlp_hint_points_at_the_official_release():
    import bootstrap

    assert "https://github.com/yt-dlp/yt-dlp/releases" in bootstrap.YTDLP_HINT
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_bootstrap.py -v
```

Expected: missing `extract_ffmpeg`.

- [ ] **Step 3: Implement ffmpeg and the yt-dlp check**

- Add `FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"` and
  `YTDLP_HINT = "yt-dlp is missing. Install it with: winget install yt-dlp.yt-dlp, or download it from https://github.com/yt-dlp/yt-dlp/releases"`.
- Add `extract_ffmpeg(archive, target_dir)`: open with `zipfile.ZipFile`, find members ending in `ffmpeg.exe` and `ffprobe.exe`, create `target_dir`, copy them out with `shutil.copyfileobj`, and return the ffmpeg path or `None`.
- `ensure_ffmpeg`: reuse an existing tool, else download to `paths.tools_dir(ROOT) / ".download" / "ffmpeg.zip"`, extract to `paths.tools_dir(ROOT) / "ffmpeg" / "bin"`, delete the archive, and keep the winget hint on failure.
- Remove `YTDLP_URL` and the `ensure_ytdlp` download. Add `ytdlp = paths.find_tool("yt-dlp", ROOT)` in `main`; when it is `None`, log `YTDLP_HINT`. The install path returns 0 with warnings so the window can still open.
- Keep `report_status` strict: a missing `yt-dlp` makes it return 1.
- Update the bootstrap docstring: it downloads mpv and ffmpeg, but not `yt-dlp`.
- Update `mv_search.search_candidates` and the two `mv_audio` fetchers to say `install it from https://github.com/yt-dlp/yt-dlp/releases` instead of `run bootstrap.py`.
- Update `cli.doctor`'s yt-dlp missing detail with the same link.

- [ ] **Step 4: Add setup.bat**

Copy the Python detection block from `run.bat`, run `bootstrap.py`, print `[setup] done; run run.bat to start`, and pause. Do not call `mvsync`.

- [ ] **Step 5: Run the tests and a manual check**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_bootstrap.py -q
.venv\Scripts\python.exe -m bootstrap --check
```

Expected: tests pass; the check reports the current machine as ready (the local `tools/` already has `yt-dlp`).

- [ ] **Step 6: Commit**

```powershell
git add bootstrap.py setup.bat tests/test_bootstrap.py src/mvsync/mv_search.py src/mvsync/mv_audio.py src/mvsync/cli.py
git commit -m "Add a setup script and leave yt-dlp to the user"
```

---

### Task 6: Public cleanup and the two-section README

**Files:**
- Modify: `.gitignore`, `pyproject.toml`, `src/mvsync/__init__.py`, `README.md`
- Delete: `AGENTS.md`, `docs/superpowers/`, `spikes/`, `requirements.lock`

**Interfaces:**
- Consumes: the public behavior from Tasks 1 to 5.
- Produces: a public tree with exactly the curated files and a README with exactly two `##` sections.

- [ ] **Step 1: Remove the developer-only files from the public branch**

```powershell
git rm -r AGENTS.md docs/superpowers spikes requirements.lock
```

- [ ] **Step 2: Update .gitignore and the package description**

Add to `.gitignore`:

```text
requirements.lock
docs/superpowers/
spikes/
```

Change the `pyproject.toml` description to:
`Play a muted YouTube music video beside the track already playing in your media player.`

Change the `src/mvsync/__init__.py` docstring to the same idea, with no Spotify-only wording.

- [ ] **Step 3: Rewrite README.md with exactly two sections**

The document must be this shape:

```markdown
# mvsync

Play a muted YouTube music video in mpv while the track you are already playing
stays the master clock.

## Install

Requirements: Windows 10 or 11 (64-bit), Python 3.12 or newer, and a media
player that appears in the Windows media controls (Spotify, VLC, MusicBee, and
similar). Per-process capture needs Windows build 20348 or newer; older builds
record the whole output device.

1. Install Python 3.12: https://www.python.org/downloads/windows/
2. Install yt-dlp yourself: `winget install yt-dlp.yt-dlp`, or download
   https://github.com/yt-dlp/yt-dlp/releases and put `yt-dlp.exe` in `tools/`.
   Setup does not download or bundle it.
3. Run `setup.bat`. It creates `.venv`, installs the Python dependencies, and
   downloads mpv and ffmpeg into `tools/`.
4. Optional: the main window's Player row picks the player. The selection is
   saved in `data/config.json`; `player`, `player_exe`, `capture_mode`, and
   `dev_log` can also be edited by hand.

mvsync does not provide music, bypass DRM, or redistribute anything. The audio
comes from the player you already use. Captured audio is local, used only to
align the video, and deleted after a successful fit. Use your own accounts and
follow the terms of the services you use.

## How to use

1. Start a song or playlist in your media player.
2. Run `run.bat`. Pick the player in the Player row if it is not selected.
3. Press `Play`. The video follows the player; a first listen plays roughly and
   is recorded so the next listen is fully synced.
4. Use `Scan` to build a map for the current track explicitly, and `Fix MV` to
   choose a different video. The picker's `Back` button returns to the main
   window.
5. During playback: `q` quits, `r` resyncs, `[` and `]` trim the offset by
   50 ms, `p` toggles the player. The mpv window keeps its size and fullscreen
   state when the video changes.
```

Do not add any other `##` heading. Fold any troubleshooting into these two
sections.

- [ ] **Step 4: Check the README shape and the tracked tree**

```powershell
(Select-String -Path README.md -Pattern '^## ').Count
git ls-files
```

Expected: `2`; and the tracked list contains no `AGENTS.md`, `docs/superpowers/`, `spikes/`, `requirements.lock`, `tools/`, or `data/`.

- [ ] **Step 5: Run the full gates**

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests bootstrap.py
```

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add -A
git commit -m "Prepare the public release tree and README"
```

---

### Task 7: Final readiness check

**Files:**
- Git only unless a check fails.

**Interfaces:**
- Consumes: everything above.
- Produces: a clean `public-release` branch ready for `git push`.

- [ ] **Step 1: Run the full gates one last time**

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check src tests bootstrap.py
git status --short
```

Expected: all tests pass, ruff passes, and the tree is clean.

- [ ] **Step 2: Inspect the public surface**

```powershell
git ls-files
git grep -n -i "spotify" -- src tests README.md pyproject.toml
```

Expected: no tracked developer-only files. `spotify` may remain only in the
alignment schema field `spotify_s`, in the legacy `capture_mode` alias and its
test, and as an example player in the README. No user-facing message should
require Spotify.

- [ ] **Step 3: Review the branch**

```powershell
git log --oneline --decorate -10
git diff --stat main..public-release
```

Expected: a linear public-release history on top of the checkpoint; `main` is
unchanged after the checkpoint.

- [ ] **Step 4: Report the push command, do not push**

The repository has no remote. Tell the human:

```powershell
git remote add origin <github-url>
git push -u origin public-release
```

Do not add a remote or push without the URL.
