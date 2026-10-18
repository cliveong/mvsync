# Spotify-Only Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record only the Spotify process's audio during scans and first listens, so notifications and other apps can play without contaminating a track's sync map.

**Architecture:** A second capture backend built on the WASAPI application-loopback API (`ActivateAudioInterfaceAsync` with `AUDIOCLIENT_ACTIVATION_PARAMS`), targeting the Spotify.exe process tree. Both backends share a new `PcmSink` so they write byte-identical WAVs. A `make_recorder` factory picks the backend from `data/config.json`, defaulting to Spotify with automatic fallback to the existing whole-device loopback when activation is unavailable.

**Tech Stack:** Python 3.12, `comtypes` + `pycaw` (new, both pure Python, no native binaries), numpy, existing `soundcard` path retained as the fallback.

**Spec:** None standalone. Decisions come from the 2026-10-18 conversation: Route A (per-process capture) chosen over per-device routing because the machine has a single output endpoint; hand-rolled COM chosen over a prebuilt DLL to keep the project binary-free; Spotify-only default with silent device fallback.

## Global Constraints

- Windows-only by design; no cross-platform shims.
- New runtime deps `comtypes>=1.2.0` and `pycaw>=20240210` go in `requirements.txt` only. `bootstrap.py` already fingerprints that file and reinstalls, so no bootstrap change is needed.
- No native binaries: `comtypes` and `pycaw` are pure Python. Do not introduce the prebuilt `process-audio-capture` package.
- Every path through `paths.py`; `data/config.json` is generated and move-safe.
- Tests run with no network, audio devices, SMTC, or mpv. Anything touching hardware goes in `spikes/` or takes the `integration` marker.
- Console text in English. This work adds the `[capture]` prefix; AGENTS.md is updated in Task 6, the last task.
- ruff line length 100; type hints on public functions; short docstrings over comments; no decorative output.
- TDD: failing test, run it, implement, run it, commit.
- A failed capture must never kill a play session or write a silent map.

## Review Focus

Failure modes most likely to bite a real user, most likely first; each has a test in the task that owns the code.

1. **Spotify has no audio session when recording starts** (app closed, or paused with no render stream). PID lookup returns nothing. The recording must be discarded, never saved as a silent map, and never raise out of `radio.start()` into the play loop (Task 3 factory test, Task 4 discard test).
2. **Spotify restarts mid-listen**, so the captured PID dies and the stream goes silent. The existing `SILENT_PEAK_DBFS` discard branch (`radio.py:159-162`) is the net, but it currently has **no test anywhere in the suite**; Task 4 adds one while wiring the new failure path, so this behaviour gains coverage instead of relying on a reviewer's memory.
3. **COM apartment conflict on import.** `comtypes` initializes STA at import and raises `RPC_E_CHANGED_MODE` if the thread is already MTA. `soundcard` imports lazily inside `default_loopback()` (`capture.py:24`), so the apartment only becomes MTA after a real device query. `sys.coinit_flags = 0` must therefore be set before *any* comtypes import and `capture_proc` must be imported before the first `default_loopback()` call. Task 3 Step 4 pins the import placement and Task 2 Step 6 proves it on this machine.
4. **`GetMixFormat` returns `E_NOTIMPL` on the virtual device and pycaw's `WAVEFORMATEX` corrupts a constructed format** (it types `nSamplesPerSec` as 16-bit, giving `E_INVALIDARG`). Our own 18-byte `_pack_ = 1` struct must be asserted by a test (Task 2 test). The same class of bug applies to `PROPVARIANT`, which must be 16 bytes on x64 with an 8-aligned union and a packed inner blob; Task 2 asserts that size too.
5. **First-listen recordings begin mid-song**, so `ready_epoch` must mark the first received packet, not activation time, or `offset_s` at `radio.py:136` silently shifts by the activation latency. This belongs to `ProcessLoopbackRecorder` (Task 2), proven by the Task 2 spike rather than a unit test.

---

### Task 1: Shared PCM sink

**Files:**
- Modify: `src/mvsync/capture.py`
- Create: `tests/test_capture.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `PcmSink(path, *, samplerate=SAMPLE_RATE, channels=CHANNELS)` with `.open()`, `.write(data: np.ndarray) -> None` taking float32 `(frames, channels)` in [-1, 1], `.close()` (idempotent), and properties `.peak_db: float`, `.frames_written: int`. `LoopbackRecorder` keeps its exact current signature and behaviour.

- [ ] **Step 1: Write the failing tests**

In the new `tests/test_capture.py`:

```python
def test_sink_writes_pcm_s16_from_float_blocks(tmp_path):
    sink = capture.PcmSink(tmp_path / "a.wav")
    sink.open()
    sink.write(np.array([[0.0, 1.0], [-1.0, 0.5]], dtype=np.float32))
    sink.close()
    with wave.open(str(tmp_path / "a.wav"), "rb") as handle:
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 48000
        assert handle.getnframes() == 2
        assert handle.readframes(2) == b"".join(
            int(v * 32767).to_bytes(2, "little", signed=True)
            for v in (0.0, 32767.0, -32767.0, 16383.5)
        )


def test_sink_tracks_peak_db(tmp_path):
    sink = capture.PcmSink(tmp_path / "a.wav")
    sink.open()
    sink.write(np.full((480, 2), 0.5, dtype=np.float32))
    sink.close()
    assert sink.peak_db == pytest.approx(-6.02, abs=0.1)


def test_sink_peak_is_minus_infinity_when_nothing_was_written(tmp_path):
    sink = capture.PcmSink(tmp_path / "a.wav")
    sink.open()
    sink.close()
    assert sink.peak_db == float("-inf")


def test_sink_clamps_out_of_range_samples(tmp_path):
    sink = capture.PcmSink(tmp_path / "a.wav")
    sink.open()
    sink.write(np.array([[4.0, -4.0]], dtype=np.float32))
    sink.close()
    with wave.open(str(tmp_path / "a.wav"), "rb") as handle:
        assert handle.readframes(1) == (
            (32767).to_bytes(2, "little", signed=True)
            + (-32767).to_bytes(2, "little", signed=True)
        )


def test_sink_close_is_idempotent(tmp_path):
    sink = capture.PcmSink(tmp_path / "a.wav")
    sink.open()
    sink.close()
    sink.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_capture.py -v`
Expected: FAIL with `AttributeError: module 'mvsync.capture' has no attribute 'PcmSink'`

- [ ] **Step 3: Implement `PcmSink` and refactor `LoopbackRecorder`**

Add `PcmSink` to `src/mvsync/capture.py`. It owns the `wave.open` handle, converts each block with the existing formula from `capture.py:84` (`np.clip(data, -1.0, 1.0) * 32767` cast to `<i2`), and folds each block through `rms_db` into a running peak. `close()` must be safe to call twice.

Then refactor `LoopbackRecorder._run` (`capture.py:69-85`) to open a `PcmSink` and call `.write()` per block instead of writing frames directly. Leave `ready_epoch` exactly where it is today (at device open, `capture.py:80`) so this task changes no behaviour.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_capture.py -v`
Expected: PASS

Run: `.venv\Scripts\python.exe -m pytest`
Expected: `155 passed` plus the new ones, no failures.

- [ ] **Step 5: Commit**

```bash
git add src/mvsync/capture.py tests/test_capture.py
git commit -m "Extract a shared PCM sink for capture backends"
```

---

### Task 2: Process loopback capture backend

**Files:**
- Create: `src/mvsync/capture_proc.py`
- Create: `spikes/spike_process_loopback.py`
- Modify: `requirements.txt`
- Modify: `tests/test_capture.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces in `src/mvsync/capture_proc.py`:
  - `is_supported(build_number: int | None = None) -> bool`. When `build_number` is `None`, read `sys.getwindowsversion().build`. True when `major > 10`, or `major == 10 and build >= 20348`.
  - `resolve_spotify_pid() -> int | None`: PID of the `spotify.exe` audio session, or `None` when Spotify has no active session.
  - `ProcessLoopbackRecorder(path, *, pid: int, samplerate: int = 48000, channels: int = 2)`: takes `pid` as a keyword and stores it in `self._pid`. The constructor opens no handle; COM is initialized inside `start()`, mirroring `LoopbackRecorder` (whose `__init__` at `capture.py:40-56` likewise only stores state and starts the thread on `start()`). `.start()`, `.stop() -> Path`, `.ready_epoch: float | None`, `.peak_db: float` match the `LoopbackRecorder` surface.
  - Module constant `SPOTIFY_PROCESS = "spotify.exe"`.

- [ ] **Step 1: Add the dependencies**

Append to `requirements.txt` in alphabetical order (`comtypes` before `librosa`, `pycaw` after `numpy`):

```
comtypes>=1.2.0
```
```
pycaw>=20240210
```

Run: `.venv\Scripts\python.exe -m pip install comtypes pycaw`
Expected: both install successfully.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_capture.py`, adding the imports the file needs (`ctypes`, `wave`, `numpy as np`, `pytest`, `from pathlib import Path`, `from mvsync import capture`):

```python
def test_is_supported_needs_build_20348():
    from mvsync import capture_proc

    assert capture_proc.is_supported(19045) is False
    assert capture_proc.is_supported(20348) is True
    assert capture_proc.is_supported(26200) is True


def test_process_structs_have_the_win32_layout():
    from mvsync import capture_proc

    assert ctypes.sizeof(capture_proc.WAVEFORMATEX) == 18
    assert ctypes.sizeof(capture_proc.AUDIOCLIENT_ACTIVATION_PARAMS) == 12
    assert ctypes.sizeof(capture_proc.PROPVARIANT) == 16


def test_process_recorder_keeps_its_pid():
    from mvsync import capture_proc

    recorder = capture_proc.ProcessLoopbackRecorder(Path("a.wav"), pid=4242)
    assert recorder._pid == 4242
```

Keep the unit surface to what needs no hardware: `is_supported`, the struct layouts, and the recorder's constructor. Everything that touches COM is proven by the spike in Step 6, not by a mock. The float32-to-int16 byte contract is already pinned by Task 1's sink tests.

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_capture.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mvsync.capture_proc'`

- [ ] **Step 4: Implement `src/mvsync/capture_proc.py`**

The module docstring states it is Windows-only, needs build 20348+, and that it records one process tree.

**The first executable statement must be `sys.coinit_flags = 0`** (`COINIT_MULTITHREADED`), placed before any `comtypes` import, because `soundcard` initializes the process into MTA at import and `comtypes` would otherwise raise `RPC_E_CHANGED_MODE`. Add a comment on that line, because removing it breaks every command in the app.

Then declare, in order:

- A correct 18-byte `_pack_ = 1` `WAVEFORMATEX` (7 fields: `wFormatTag`, `nChannels`, `nSamplesPerSec`, `nAvgBytesPerSec`, `nBlockAlign`, `wBitsPerSample`, `cbSize`). Do not use pycaw's.
- `AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS` (`TargetProcessId`, `ProcessLoopbackMode`) and `AUDIOCLIENT_ACTIVATION_PARAMS` (`ActivationType`, `ProcessLoopbackParams`).
- A minimal `PROPVARIANT` sized **16 bytes on x64**, with an 8-byte-aligned union and a `_pack_ = 1` inner `_BLOB` (`cbSize`, `pBlobData`). Get this wrong and the blob pointer lands misaligned, which surfaces as `E_INVALIDARG` or garbage from `ActivateAudioInterfaceAsync`. The Step 2 test pins the size.
- COM interfaces pycaw omits, with these IIDs and vtables:
  - `IAgileObject` `{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}`, no methods.
  - `IActivateAudioInterfaceAsyncOperation` `{72A22D78-CDE4-431D-B8CC-843A71199B6D}`, method `GetActivateResult(HRESULT* out, IUnknown** activated)`.
  - `IActivateAudioInterfaceCompletionHandler` `{41D949AB-9862-444A-80F6-C261334DA5EB}`, method `ActivateCompleted(IActivateAudioInterfaceAsyncOperation*)`.
  - `IAudioCaptureClient` `{C8ADBD64-E71E-48A0-A4DE-185C395CD317}`, methods `GetBuffer`, `ReleaseBuffer`, `GetNextPacketSize` in that vtable order.
- A `_CompletionHandler(COMObject)` whose `_com_interfaces_` list is exactly `[IActivateAudioInterfaceCompletionHandler, IAgileObject]` and whose `ActivateCompleted` sets a `threading.Event` and returns `S_OK`.

Constants: `AUDCLNT_SHAREMODE_SHARED = 0`, `AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000`, `AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000`, `AUDCLNT_BUFFERFLAGS_SILENT = 0x2`, `WAVE_FORMAT_IEEE_FLOAT = 0x0003`, `VT_BLOB = 0x41`, `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1`, `PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0`, `VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"`.

`resolve_spotify_pid()`: enumerate audio sessions across all active render endpoints via pycaw (`IAudioSessionControl2.ProcessId` and `Process.name()`), returning the first PID whose name matches `SPOTIFY_PROCESS` case-insensitively, else `None`. Wrap the whole enumeration in `try/except Exception` returning `None`, so a missing dependency or a refused endpoint never raises.

`ProcessLoopbackRecorder`: mirror `LoopbackRecorder`, including the daemon thread (name it `"process-recorder"`) and the `ready_epoch` / `peak_db` attributes. In the thread: `CoInitializeEx(NULL, COINIT_MULTITHREADED)`, build the activation `PROPVARIANT`, call `ActivateAudioInterfaceAsync`, wait on the handler event with a 3 s timeout, `QueryInterface(IAudioClient)`, `Initialize(AUDCLNT_SHAREMODE_SHARED, LOOPBACK|EVENTCALLBACK, 0, 0, wfx_ptr, None)`, `SetEventHandle`, `GetService(IAudioCaptureClient)`, `Start()`. Then loop: wait on the event (200 ms), drain `GetNextPacketSize` / `GetBuffer` / `ReleaseBuffer`, honour `AUDCLNT_BUFFERFLAGS_SILENT` by writing zeros, convert float32 interleaved data to int16 through `PcmSink`, carry leftover frames to the next read, and set `ready_epoch = time.time()` on the first non-empty block (Review Focus 5). `stop()` joins the thread, then `Stop()`, `CloseHandle`, `CoUninitialize`, all guarded, and returns the path.

Any exception inside the thread must be stored and re-raised by `stop()` as `RuntimeError`, so the caller can discard the recording instead of getting a silent file.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_capture.py -v`
Expected: PASS

- [ ] **Step 6: Write and run the spike before trusting the code**

Create `spikes/spike_process_loopback.py` following `spikes/spike_loopback.py`: same header docstring style, same `sys.path.insert` bootstrap, same print layout, returns 1 when the peak is below -60 dBFS. It resolves the Spotify PID, reports it, records 5 seconds through `ProcessLoopbackRecorder` into `data/recordings/spike-process.wav`, and prints the peak.

Run with Spotify actively playing: `.venv\Scripts\python.exe spikes\spike_process_loopback.py`
Expected: exit 0, a PID printed, a peak above -60 dBFS, and a 5 second WAV.

**If the spike fails, stop and reassess** before Task 3. Do not debug through to Task 4.

- [ ] **Step 7: Commit**

```bash
git add src/mvsync/capture_proc.py spikes/spike_process_loopback.py requirements.txt tests/test_capture.py
git commit -m "Add a WASAPI process loopback capture backend"
```

---

### Task 3: Recorder factory and capture-mode config

**Files:**
- Create: `src/mvsync/config.py`
- Modify: `src/mvsync/paths.py`, `src/mvsync/capture.py`
- Modify: `tests/test_capture.py`

**Interfaces:**
- Consumes: `PcmSink` (Task 1), `ProcessLoopbackRecorder`, `resolve_spotify_pid`, `is_supported` (Task 2).
- Produces:
  - `paths.config_path(root=None) -> Path` returning `data_dir(root) / "config.json"`.
  - `config.load_capture_mode(root=None) -> str`, `"spotify"` when the file is missing, malformed, or holds an unknown value.
  - `config.set_capture_mode(mode: str, root=None) -> None`, `ValueError` on anything but `"spotify"` / `"device"`, written atomically via `os.replace` of a sibling temp file (mirror `align_store.save_alignment`, `align_store.py:80-90`).
  - `capture.make_recorder(path, *, root=None, mode=None) -> LoopbackRecorder | ProcessLoopbackRecorder`. `mode` defaults to `config.load_capture_mode(root)`. Returns the device recorder for `"device"`. For `"spotify"` returns the process recorder when `is_supported()` and `resolve_spotify_pid()` both succeed, otherwise prints one `[capture]` line naming the reason and returns the device recorder.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capture.py`, adding whatever imports the file still lacks (`from mvsync import capture, config, paths`):

```python
def test_capture_mode_defaults_to_spotify(tmp_path):
    assert config.load_capture_mode(tmp_path) == "spotify"


def test_capture_mode_round_trips(tmp_path):
    config.set_capture_mode("device", tmp_path)
    assert config.load_capture_mode(tmp_path) == "device"


def test_capture_mode_ignores_malformed_json(tmp_path):
    paths.ensure_dir(tmp_path / "data")
    (tmp_path / "data" / "config.json").write_text("{not json", encoding="utf-8")
    assert config.load_capture_mode(tmp_path) == "spotify"


def test_set_capture_mode_rejects_unknown_values(tmp_path):
    with pytest.raises(ValueError):
        config.set_capture_mode("stereo", tmp_path)


def test_make_recorder_falls_back_when_spotify_is_missing(tmp_path, capsys, monkeypatch):
    from mvsync import capture, capture_proc

    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_spotify_pid", lambda: None)
    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path)
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "[capture]" in capsys.readouterr().out


def test_make_recorder_uses_the_process_backend(tmp_path, monkeypatch):
    from mvsync import capture, capture_proc

    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_spotify_pid", lambda: 4242)
    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path)
    assert isinstance(recorder, capture.ProcessLoopbackRecorder)
    assert recorder._pid == 4242
```

The monkeypatches target `capture_proc`, not `capture`, because `make_recorder` imports it lazily (see Step 4).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_capture.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mvsync.config'`

- [ ] **Step 3: Implement `config.py` and `paths.config_path`**

`config_path` sits next to the other `data_dir` children in `paths.py`. `config.py` is stdlib-only: `json.loads` inside `try/except (OSError, ValueError)` returning `"spotify"` on any failure, and `set_capture_mode` validating before writing.

- [ ] **Step 4: Implement `make_recorder` in `capture.py`**

Import `config` at module top. Import `capture_proc` **lazily inside `make_recorder`**, never at the top of `capture.py`. This is deliberate and not negotiable: `radio.py:21` and `scan_runtime.py:27` import `capture` at module scope, so a top-level `from mvsync.capture_proc import ...` would pull `comtypes` in whenever either module loads, breaking `cli.py`'s lazy-heavy-import rule (`cli.py:1-4`) and re-opening the COM apartment race (Review Focus 3).

So the tests must patch through `capture_proc`, not through `capture`. Write them as:

```python
def test_make_recorder_falls_back_when_spotify_is_missing(tmp_path, capsys, monkeypatch):
    from mvsync import capture, capture_proc

    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_spotify_pid", lambda: None)
    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path)
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "[capture]" in capsys.readouterr().out


def test_make_recorder_uses_the_process_backend(tmp_path, monkeypatch):
    from mvsync import capture, capture_proc

    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_spotify_pid", lambda: 4242)
    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path)
    assert isinstance(recorder, capture.ProcessLoopbackRecorder)
    assert recorder._pid == 4242
```

and have `make_recorder` reference `capture_proc.is_supported` and `capture_proc.resolve_spotify_pid` through the module object, so those patches take effect. Wrap the import in `try/except ImportError` and treat an import failure as one more fallback reason.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_capture.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/mvsync/config.py src/mvsync/paths.py src/mvsync/capture.py tests/test_capture.py
git commit -m "Add capture mode config and the recorder factory"
```

---

### Task 4: Wire the runtime callers

**Files:**
- Modify: `src/mvsync/radio.py:114`, `src/mvsync/scan_runtime.py:147`, `src/mvsync/scan_runtime.py:163-167`
- Modify: `tests/test_scan_runtime.py:297,336,393`, `tests/test_fit.py`

**Interfaces:**
- Consumes: `make_recorder(path, *, root=None, mode=None)` (Task 3).
- Produces: `RadioRecording.__init__` gains `capture_mode: str | None = None`. No other public signature changes.

- [ ] **Step 1: Repoint the existing fakes to the new seam**

`tests/test_scan_runtime.py` monkeypatches `scan_runtime.LoopbackRecorder` in three places (lines 297, 336, 393). Change all three to `scan_runtime.make_recorder`, with the lambda signature `(path, *, root=None, mode=None)` returning `FakeRecorder(path, clock)`. Keep the existing assertions intact so this step must stay green before the next one.

- [ ] **Step 2: Run the suite to confirm the red state**

Run: `.venv\Scripts\python.exe -m pytest tests/test_scan_runtime.py -q`
Expected: FAIL. The tests now install a fake `make_recorder` while production still calls `LoopbackRecorder`, so the real recorder tries to open a sound device. That failure is the red state for Step 3; do not fix it by restoring the old monkeypatch.

- [ ] **Step 3: Switch production to `make_recorder`**

`scan_runtime._record_and_fit`: replace `LoopbackRecorder(rec_path)` at line 147 with `make_recorder(rec_path, root=root)`. Wrap construction so a `RuntimeError` becomes `ScanAborted` with the same message shape as the existing branch at lines 151-153. Drop the now-unused `LoopbackRecorder` import at `scan_runtime.py:27`, or the ruff `F` rules selected in `pyproject.toml:24` fail on `F401`.

`radio.py`: move recorder construction **out of `__init__` (line 114) and into `start()`**, because the test in Step 4 patches `radio.make_recorder` after construction and would otherwise have nothing left to intercept. Set `self._recorder = None` in `__init__` and drop the `LoopbackRecorder` import at `radio.py:21`.

Inside `start()`, wrap construction in `try/except RuntimeError`, and on failure set `self._recorder = None` and return **without** setting `self._started = True` (today it is set first at `radio.py:128`). `finish()` already early-returns on `not self._started` (`radio.py:150-152`) and calls `_discard(self._rec_path)`, so the existing branch becomes the new failure path for free; add one `[play]` line there naming the reason. Print the line in `start()` instead, where the error is known, and keep `finish()` untouched.

This is Review Focus 1: a play session must survive a failed capture, and `self._recorder` must never be `None` when `finish()` reaches `self._recorder.stop()` at line 153.

- [ ] **Step 4: Write the failing test for a missing Spotify stream**

In `tests/test_fit.py`, following its existing `RadioRecording` fixture style. This test also covers Review Focus 2 (a dead PID yields a silent stream that must be discarded), so assert the silent branch too:

```python
def test_first_listen_discards_when_the_recorder_cannot_start(tmp_path, monkeypatch):
    from mvsync import radio as radio_module
    from mvsync import paths

    rec_path = paths.ensure_dir(tmp_path / "rec") / "a.wav"
    recording = radio_module.RadioRecording(
        rec_path=rec_path,
        candidates=[],
        track_id="t",
        artist="a",
        title="t",
        track_duration_s=100.0,
        root=tmp_path,
    )
    monkeypatch.setattr(
        radio_module,
        "make_recorder",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no spotify process")),
    )
    recording.start(position_s=0.0, wall=time.time())
    recording.finish()
    assert not rec_path.exists()


def test_first_listen_discards_a_silent_recording(tmp_path, monkeypatch):
    from mvsync import radio as radio_module
    from mvsync import paths

    rec_path = paths.ensure_dir(tmp_path / "rec") / "a.wav"
    recording = radio_module.RadioRecording(
        rec_path=rec_path,
        candidates=[],
        track_id="t",
        artist="a",
        title="t",
        track_duration_s=100.0,
        root=tmp_path,
    )
    monkeypatch.setattr(radio_module, "make_recorder", lambda *a, **k: SilentRecorder(rec_path))
    recording.start(position_s=0.0, wall=time.time())
    recording.finish()
    assert not rec_path.exists()
```

where `SilentRecorder` is a local stand-in with `start()`, `stop()`, `peak_db = -90.0`, and `ready_epoch = time.time()`. Do not pass `capture_mode`: `RadioRecording` reads it from config, and the point of these tests is the failure path, not the mode.

- [ ] **Step 5: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_fit.py -v`
Expected: FAIL. The first error is `TypeError` on the recorder construction path: the current `__init__` builds the recorder at `radio.py:114`, so `make_recorder` is not called at all and the monkeypatch is inert. Both tests fail for the same reason, which is the red state for Step 3.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_fit.py -v`
Expected: PASS

- [ ] **Step 7: Update the silence hint**

At `scan_runtime.py:163-167`, branch the "check that Spotify plays through the default output device" message on the active backend: with the process recorder, say Spotify had no audio stream (app closed, or paused with nothing rendering); with the device recorder, keep today's wording. Read the mode once via `config.load_capture_mode`.

- [ ] **Step 8: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/mvsync/radio.py src/mvsync/scan_runtime.py tests/test_fit.py tests/test_scan_runtime.py
git commit -m "Record only Spotify when per-process capture is available"
```

---

### Task 5: Doctor visibility

**Files:**
- Modify: `src/mvsync/cli.py:56-62`
- Create: `tests/test_cli.py` (no doctor test exists anywhere in the suite today, so this file is new)

**Interfaces:**
- Consumes: `config.load_capture_mode`, `capture.is_supported`, `capture.resolve_spotify_pid`.
- Produces: one extra `[doctor]` line and a module-level `_doctor_capture_report() -> None` in `cli.py`.

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
def test_doctor_reports_the_active_capture_backend(capsys):
    from mvsync import cli

    cli.doctor()
    out = capsys.readouterr().out
    assert "[doctor] capture:" in out
```

Assert on the real line. Do not monkeypatch `_doctor_capture_report` away and then assert on output that nothing produces: `doctor()` prints `python >= 3.12`, four tool names, `spotify session`, `loopback device`, `librosa`, and the summary, none of which contain the substring `capture`. The check must be allowed to run for real, so monkeypatch `capture.is_supported` and `capture.resolve_spotify_pid` to fakes, and monkeypatch the other heavyweight probes (`SpotifyBridge`, `default_loopback`, `librosa`) to fakes, so the test needs no hardware and no network.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -v`
Expected: FAIL, the line is absent.

- [ ] **Step 3: Implement the doctor check**

Add `_doctor_capture_report()` in `cli.py`, called from `doctor()` next to the existing loopback check, printing one `[doctor] capture: ...` line with the configured mode, whether the OS supports process loopback, the resolved Spotify PID (or "no audio session"), and the backend actually in use. Keep the existing `loopback device` line so a fallback regression stays visible. The function is module-level and returns `None` so tests can monkeypatch it; it must not raise, since doctor reports rather than fails.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cli.py -v`
Expected: PASS

Run: `.venv\Scripts\python.exe -m pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mvsync/cli.py tests/test_cli.py
git commit -m "Report the active capture backend in doctor"
```

---

### Task 6: Docs and final gates

**Files:**
- Modify: `README.md`, `AGENTS.md`

- [ ] **Step 1: Update the README**

Replace the limitation bullet "The loopback records everything the default output device plays, so keep notifications, games, and other apps quiet during a first listen or scan." with a description of Spotify-only capture, the `data/config.json` switch (`{"capture.mode": "device"}` restores whole-mix capture), the automatic fallback, and the `comtypes`/`pycaw` dependency note. Add `data/config.json` to the "What lands on disk" tree and mention the capture line in the Doctor section.

Also correct the now-false bullet at README.md:178-182, which claims Spotify "must play through the default output device (the loopback records that device)" and that switching devices with Spotify Connect voids a first listen. With the process backend that is no longer true: capture follows the Spotify process regardless of output device. Keep the Spotify Connect caveat only for `capture.mode = "device"`.

- [ ] **Step 2: Update AGENTS.md**

Add `[capture]` to the console-prefix invariant. Add an invariant: scans and first listens capture only the Spotify process tree on build 20348+ via `capture_proc`, falling back to the whole-device loopback, and `ready_epoch` must be stamped on the first captured packet rather than at activation, or `offset_s` shifts. Add `capture_proc` and `config` to the IO adapters list. Note the COM apartment constraint (`sys.coinit_flags = 0` before importing `comtypes`) as an invariant, since removing it breaks every command.

- [ ] **Step 3: Run the full gates**

Run: `.venv\Scripts\python.exe -m pytest`
Expected: all pass.

Run: `.venv\Scripts\python.exe -m ruff check src tests spikes bootstrap.py`
Expected: `All checks passed!`

- [ ] **Step 4: Manual end-to-end check**

With a browser tab playing music and a notification firing mid-song, run `run.bat scan` on a known track. Expected: the scan succeeds, the map is sane, and `run.bat doctor` shows the Spotify backend with a PID.

- [ ] **Step 5: Commit**

```bash
git add README.md AGENTS.md
git commit -m "Document Spotify-only capture"
```

---

## Notes for the implementer

- Task 2 Step 6 (the spike) is the only genuinely risky step. If it fails, stop and reassess rather than debugging through Tasks 3 to 6.
- Task 4 Step 2 is expected to fail on the first run: repointing the monkeypatch without switching production code breaks the fakes. The failure lands in microseconds rather than after the 3 s grace, because `test_scan_runtime.py` replaces `scan_runtime.time` with a `StepClock` (lines 293, 391), so `_wait_until`'s grace at `scan_runtime.py:151` is measured in fake time. That is the red state for Step 3; do not restore the old monkeypatch.
- `capture.py` has no direct unit tests today. Task 1 adds the first, which is why the `PcmSink` refactor is sequenced before any new backend.
- Five details in Task 2 are each individually capable of failing confusingly: the `sys.coinit_flags` ordering, `PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0`, the six-argument `Initialize` with two zero durations, pycaw's corrupt `WAVEFORMATEX`, and the x64 `PROPVARIANT` alignment. The struct-size tests in Task 2 Step 2 pin the last two.