"""Windows-only WASAPI process-loopback capture for one process tree.

Requires Windows 10 build 20348 or later. Records only the audio rendered by
the target process (and its children), leaving notifications and other apps
out of the recording.
"""

# ruff: noqa: E402

from __future__ import annotations

import sys

# Must run before any comtypes import: soundcard may already have put this
# process into the MTA, and comtypes defaults the importing thread to STA.
sys.coinit_flags = 0

import contextlib
import ctypes
import threading
import time
from ctypes import (
    POINTER,
    Structure,
    Union,
    byref,
    c_long,
    c_ubyte,
    c_uint32,
    c_uint64,
    c_ulong,
    c_ushort,
    c_void_p,
)
from ctypes.wintypes import DWORD, HANDLE, LPCWSTR, WORD
from pathlib import Path, PureWindowsPath

import numpy as np
from comtypes import COMMETHOD, GUID, HRESULT, COMObject, IUnknown
from pycaw.api.audioclient import IAudioClient

from .capture import CHANNELS, SAMPLE_RATE, PcmSink

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_IEEE_FLOAT = 0x0003
VT_BLOB = 0x41
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
ACTIVATION_TIMEOUT_S = 3.0
PACKET_WAIT_MS = 200
BYTES_PER_SAMPLE = 4


class WAVEFORMATEX(Structure):
    """The real 18-byte WAVEFORMATEX; pycaw's has 32-bit rate fields."""

    _pack_ = 1
    _fields_ = [
        ("wFormatTag", WORD),
        ("nChannels", WORD),
        ("nSamplesPerSec", DWORD),
        ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", WORD),
        ("wBitsPerSample", WORD),
        ("cbSize", WORD),
    ]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(Structure):
    _fields_ = [
        ("TargetProcessId", DWORD),
        ("ProcessLoopbackMode", c_long),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
    _fields_ = [
        ("ActivationType", c_long),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class _BLOB(Structure):
    _fields_ = [
        ("cbSize", c_ulong),
        ("pBlobData", c_void_p),
    ]


class _PROPVARIANT_UNION(Union):
    _fields_ = [("blob", _BLOB)]


class PROPVARIANT(Structure):
    """Native x64 PROPVARIANT: 8-byte header plus a 16-byte BLOB union."""

    _fields_ = [
        ("vt", c_ushort),
        ("wReserved1", c_ushort),
        ("wReserved2", c_ushort),
        ("wReserved3", c_ushort),
        ("union", _PROPVARIANT_UNION),
    ]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = ()


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "GetActivateResult",
            (["out"], POINTER(HRESULT), "activateResult"),
            (["out"], POINTER(POINTER(IUnknown)), "activatedInterface"),
        ),
    )


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "ActivateCompleted",
            (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation"),
        ),
    )


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
            (["out"], POINTER(c_uint32), "pNumFramesToRead"),
            (["out"], POINTER(DWORD), "pdwFlags"),
            (["out"], POINTER(c_uint64), "pu64DevicePosition"),
            (["out"], POINTER(c_uint64), "pu64QPCPosition"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "ReleaseBuffer",
            (["in"], c_uint32, "NumFramesRead"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetNextPacketSize",
            (["out"], POINTER(c_uint32), "pNumFramesInNextPacket"),
        ),
    )


class _CompletionHandler(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self) -> None:
        self.done = threading.Event()

    def ActivateCompleted(self, activate_operation) -> int:
        self.done.set()
        return 0


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateEventW.argtypes = [c_void_p, ctypes.c_int, ctypes.c_int, LPCWSTR]
_kernel32.CreateEventW.restype = HANDLE
_kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
_kernel32.WaitForSingleObject.restype = DWORD
_kernel32.CloseHandle.argtypes = [HANDLE]
_kernel32.CloseHandle.restype = ctypes.c_int

_mmdevapi = ctypes.WinDLL("mmdevapi")
_ActivateAudioInterfaceAsync = _mmdevapi.ActivateAudioInterfaceAsync
_ActivateAudioInterfaceAsync.argtypes = [
    LPCWSTR,
    POINTER(GUID),
    POINTER(PROPVARIANT),
    POINTER(IActivateAudioInterfaceCompletionHandler),
    POINTER(POINTER(IActivateAudioInterfaceAsyncOperation)),
]
_ActivateAudioInterfaceAsync.restype = HRESULT


def is_supported(build_number: int | None = None) -> bool:
    """Whether the process-loopback activation API is available."""
    if build_number is None:
        version = sys.getwindowsversion()
        return version.major > 10 or (version.major == 10 and version.build >= 20348)
    return build_number >= 20348


def resolve_player_pid(exe_name: str | None) -> int | None:
    """PID of the named player's audio session, or ``None`` when it has none."""
    target = (exe_name or "").strip().lower()
    if not target:
        return None
    target = PureWindowsPath(target).name
    if not target.endswith(".exe"):
        target += ".exe"
    try:
        import psutil
        from pycaw.api.audiopolicy import IAudioSessionControl2
        from pycaw.constants import DEVICE_STATE, EDataFlow
        from pycaw.utils import AudioUtilities

        devices = AudioUtilities.GetAllDevices(
            EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value
        )
        for device in devices:
            if device is None:
                continue
            try:
                sessions = device.AudioSessionManager.GetSessionEnumerator()
                for index in range(sessions.GetCount()):
                    control = sessions.GetSession(index)
                    if control is None:
                        continue
                    session = control.QueryInterface(IAudioSessionControl2)
                    pid = int(session.GetProcessId())
                    if pid == 0:
                        continue
                    try:
                        name = psutil.Process(pid).name()
                    except Exception:
                        continue
                    if name.lower() == target:
                        return pid
            except Exception:
                continue
    except Exception:
        return None
    return None


def _make_format(samplerate: int, channels: int) -> WAVEFORMATEX:
    block_align = channels * (32 // 8)
    return WAVEFORMATEX(
        wFormatTag=WAVE_FORMAT_IEEE_FLOAT,
        nChannels=channels,
        nSamplesPerSec=samplerate,
        nAvgBytesPerSec=samplerate * block_align,
        nBlockAlign=block_align,
        wBitsPerSample=32,
        cbSize=0,
    )


def _make_activation(pid: int) -> tuple[PROPVARIANT, AUDIOCLIENT_ACTIVATION_PARAMS]:
    activation = AUDIOCLIENT_ACTIVATION_PARAMS(
        ActivationType=AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        ProcessLoopbackParams=AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(
            TargetProcessId=pid,
            ProcessLoopbackMode=PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
        ),
    )
    propvariant = PROPVARIANT()
    propvariant.vt = VT_BLOB
    propvariant.union.blob.cbSize = ctypes.sizeof(activation)
    propvariant.union.blob.pBlobData = ctypes.cast(byref(activation), c_void_p)
    return propvariant, activation


class ProcessLoopbackRecorder:
    """Records one process tree to a WAV file on a worker thread."""

    backend = "process"

    def __init__(
        self,
        path: Path,
        *,
        pid: int,
        samplerate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
    ) -> None:
        self.path = path
        self.samplerate = samplerate
        self.channels = channels
        self._pid = pid
        self.peak_db = float("-inf")
        self.ready_epoch: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._client = None
        self._event_handle: int | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="process-recorder", daemon=True)
        self._thread.start()

    def stop(self) -> Path:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                raise RuntimeError("process recorder did not stop within 5 seconds")
        if self._error is not None:
            raise RuntimeError(f"process loopback capture failed: {self._error!r}") from self._error
        return self.path

    def _run(self) -> None:
        self._client = None
        initialized = False
        sink: PcmSink | None = None
        try:
            import comtypes

            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            initialized = True
            sink = PcmSink(self.path, samplerate=self.samplerate, channels=self.channels)
            sink.open()
            self._client = self._activate()
            self._event_handle = _kernel32.CreateEventW(None, False, False, None)
            if not self._event_handle:
                raise OSError(ctypes.get_last_error(), "CreateEventW failed")
            self._capture = self._client.GetService(IAudioCaptureClient._iid_).QueryInterface(
                IAudioCaptureClient
            )
            self._client.SetEventHandle(self._event_handle)
            self._client.Start()
            self._loop(sink)
        except BaseException as exc:
            self._error = exc
        finally:
            close_error: BaseException | None = None
            try:
                if self._client is not None:
                    with contextlib.suppress(Exception):
                        self._client.Stop()
                if self._event_handle:
                    _kernel32.CloseHandle(self._event_handle)
                    self._event_handle = None
            finally:
                if sink is not None:
                    try:
                        sink.close()
                    except BaseException as exc:
                        close_error = exc
                self._client = None
                self._capture = None
                if initialized:
                    import comtypes

                    comtypes.CoUninitialize()
            if close_error is not None and self._error is None:
                self._error = RuntimeError(f"WAV finalization failed: {close_error!r}")

    def _activate(self):
        handler = _CompletionHandler()
        propvariant, activation = _make_activation(self._pid)
        operation = POINTER(IActivateAudioInterfaceAsyncOperation)()
        _ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            byref(IAudioClient._iid_),
            byref(propvariant),
            handler,
            byref(operation),
        )
        if not handler.done.wait(ACTIVATION_TIMEOUT_S):
            raise TimeoutError("audio interface activation timed out")
        if not operation:
            raise RuntimeError("activation returned no operation")
        activate_result, activated = operation.GetActivateResult()
        if activate_result != 0:
            raise OSError(activate_result, "ActivateAudioInterfaceAsync failed")
        if not activated:
            raise RuntimeError("activation returned no interface")
        client = activated.QueryInterface(IAudioClient)
        wfx = _make_format(self.samplerate, self.channels)
        from pycaw.api.audioclient.depend import WAVEFORMATEX as PycawWaveFormat

        client.Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
            0,
            0,
            ctypes.cast(byref(wfx), POINTER(PycawWaveFormat)),
            None,
        )
        return client

    def _loop(self, sink: PcmSink) -> None:
        while not self._stop.is_set():
            result = _kernel32.WaitForSingleObject(self._event_handle, PACKET_WAIT_MS)
            if result == WAIT_TIMEOUT:
                continue
            if result != WAIT_OBJECT_0:
                raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
            while not self._stop.is_set():
                frames = int(self._capture.GetNextPacketSize())
                if frames == 0:
                    break
                data, _, flags, _, _ = self._capture.GetBuffer()
                try:
                    if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                        block = np.zeros((frames, self.channels), dtype=np.float32)
                    else:
                        raw = ctypes.string_at(data, frames * self.channels * BYTES_PER_SAMPLE)
                        block = np.frombuffer(raw, dtype=np.float32).reshape(
                            frames, self.channels
                        )
                finally:
                    self._capture.ReleaseBuffer(frames)
                if frames:
                    if self.ready_epoch is None:
                        self.ready_epoch = time.time()
                    sink.write(block)
                    self.peak_db = sink.peak_db
