import json
import os
import subprocess
import sys
import threading
from functools import partial

import pytest

from mvsync.gui import MAIN_ACTIONS
from mvsync.gui_console import KeySource, LogSink


class FakeButton:
    """Stand-in for a ttk.Button: configure records the last state given."""

    def __init__(self):
        self.state = "normal"

    def configure(self, **kwargs):
        self.state = kwargs.get("state", self.state)


class FakeText:
    """Stand-in for a read-only tk.Text: insert appends, configure tracks state."""

    def __init__(self):
        self.value = ""
        self.state = "disabled"

    def configure(self, **kwargs):
        self.state = kwargs.get("state", self.state)

    def insert(self, index, text):
        self.value += text

    def see(self, index):
        pass


class FakeWidget:
    """Stand-in for a ttk widget: configure and pack record the call, never draw."""

    def __init__(self):
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)

    def pack(self, **kwargs):
        self.options["packed"] = True

    def pack_forget(self):
        self.options["packed"] = False

    def focus_set(self):
        self.options["focused"] = True


class FakeProgress:
    """Stand-in for ttk.Progressbar: records start, stop, and packing."""

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.packed = False

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def pack(self, **kwargs):
        self.packed = True

    def pack_forget(self):
        self.packed = False


class FakeEvent:
    """Only the two attributes MainWindow._on_key reads, so no Tk root is needed."""

    def __init__(self, keysym, widget=None):
        self.keysym = keysym
        self.widget = widget


class FakeBridge:
    """SMTC stand-in reporting no session, so refresh_track needs no player."""

    def current(self):
        return None


class StandInWindow:
    """Only the attributes MainWindow._on_key, ._pump and ._scan_candidates read."""

    def __init__(self, *, closing=False, sink=None, state=None):
        self.url_entry = object()
        self.other_widget = object()
        self._keys = KeySource()
        self._key_consumers = (self.url_entry,)
        self._log_text = FakeText()
        self._sink = sink if sink is not None else LogSink()
        self._closing = closing
        self._state = state
        self._pump_after = None
        self._root_dir = None
        self._players = []
        self._busy = False
        self._action = None
        self._locked = False
        self._lock_pid = 0
        self._video_switches = None
        self._picker_open = False
        self._picker_track_id = None
        self._picker_candidates = []
        self._picker_current_video = None
        self._picker_busy = False
        self._notice = None
        self._notice_label = None
        self._notice_kind = None
        self._refresh_after = None
        self.scheduled = []
        self.cancelled = []

    def after(self, delay_ms, callback, *args):
        """Record every call with its extra args, the way Tk's after passes them on."""
        handle = f"after#{len(self.scheduled) + 1}"
        self.scheduled.append((delay_ms, callback, *args))
        return handle

    def after_cancel(self, handle):
        self.cancelled.append(handle)

    def _selected_index(self):
        from mvsync.gui import MainWindow

        return MainWindow._selected_index(self)

    def _selected_candidate(self):
        from mvsync.gui import MainWindow

        return MainWindow._selected_candidate(self)

    def _auto_refresh(self):
        from mvsync.gui import MainWindow

        return MainWindow._auto_refresh(self)

    def _use_candidate(self, candidate):
        from mvsync.gui import MainWindow

        return MainWindow._use_candidate(self, candidate)


class ThreadCheckedUrlVar:
    """Stand-in for tk.StringVar: get() refuses to run off the thread that made it.

    Tk raises the same RuntimeError from a StringVar touched off the main thread,
    so a test can prove the read happened on the Tk thread without a Tk root.
    """

    def __init__(self, value):
        self.owner = threading.current_thread()
        self.reads = []
        self.value = value

    def get(self):
        self.reads.append(threading.current_thread())
        if threading.current_thread() is not self.owner:
            raise RuntimeError("main thread is not in main loop")
        return self.value


def pump_stand_in(**kwargs):
    """A stand-in whose _pump is the real one, since the pump reschedules itself."""
    from mvsync.gui import MainWindow

    window = StandInWindow(**kwargs)
    window._pump = partial(MainWindow._pump, window)
    return window


def lock_stand_in(root):
    """A stand-in whose lock probe and the handlers around it are the real ones."""
    from mvsync.gui import MainWindow

    window = StandInWindow()
    window._busy = False
    window._action = None
    window._locked = False
    window._lock_pid = 0
    window._root_dir = root
    window._buttons = {action: FakeButton() for action in MAIN_ACTIONS}
    window._track_label = FakeWidget()
    window._status_label = FakeWidget()
    window._status_dot = FakeWidget()
    window._hotkey_row = FakeWidget()
    window._log = partial(MainWindow._log, window)
    window._refresh_lock = partial(MainWindow._refresh_lock, window)
    window._update_buttons = partial(MainWindow._update_buttons, window)
    window._set_status = partial(MainWindow._set_status, window)
    window._update_status = partial(MainWindow._update_status, window)
    window._show_notice = partial(MainWindow._show_notice, window)
    window._clear_notice = partial(MainWindow._clear_notice, window)
    window._refresh_sync_badge = partial(MainWindow._refresh_sync_badge, window)
    window._set_hotkeys_visible = partial(MainWindow._set_hotkeys_visible, window)
    window._cancel_pump = partial(MainWindow._cancel_pump, window)
    window._set_progress = partial(MainWindow._set_progress, window)
    window._run_finished = partial(MainWindow._run_finished, window)
    window._pump = partial(MainWindow._pump, window)
    window._worker = None
    return window


def run_stand_in(root):
    """A stand-in whose _run is the real one, so a worker thread actually starts."""
    from mvsync.gui import MainWindow

    window = lock_stand_in(root)
    window._run = partial(MainWindow._run, window)
    return window


def join_worker(window, timeout=5.0):
    """Wait for the worker _run started, failing loudly if it is still running."""
    assert window._worker is not None, "no worker thread was started"
    window._worker.join(timeout=timeout)
    assert not window._worker.is_alive(), "the worker thread outlived its join timeout"


def test_gui_module_exposes_run_fixer():
    from mvsync import gui

    assert callable(gui.run_fixer)


def test_format_length():
    from mvsync.gui import format_length

    assert format_length(None) == "?"
    assert format_length(221.0) == "3:41"


def test_now_playing_values_shape_the_header():
    from mvsync.gui import now_playing_values
    from mvsync.player_smtc import PlayerState

    state = PlayerState(
        title="Psycho",
        artist="Red Velvet",
        duration_s=221.0,
        position_s=12.0,
        updated_epoch_s=100.0,
        playing=True,
    )
    assert now_playing_values(state) == ("Psycho", "Red Velvet", "3:41 · Playing")


def test_now_playing_values_explain_an_empty_session():
    from mvsync.gui import now_playing_values

    assert now_playing_values(None) == (
        "Nothing playing yet",
        "Start a song in Spotify, then press Play.",
        "No active player",
    )


def test_status_tone_prioritizes_busy_over_locked():
    from mvsync.gui import status_tone

    assert status_tone(busy=True, locked=True) == "busy"
    assert status_tone(busy=False, locked=True) == "locked"
    assert status_tone(busy=False, locked=False) == "idle"


def test_hotkey_hint_labels_the_controls():
    from mvsync.gui import hotkey_hint

    assert hotkey_hint("q") == "Stop"
    assert hotkey_hint("r") == "Resync"
    assert hotkey_hint("[") == "Trim -"
    assert hotkey_hint("]") == "Trim +"
    assert hotkey_hint("p") == "Player"


def test_progress_bar_runs_only_while_busy():
    from mvsync.gui import MainWindow

    window = StandInWindow()
    window._progress = FakeProgress()

    MainWindow._set_progress(window, True)
    assert window._progress.started == 1
    assert window._progress.packed is True

    MainWindow._set_progress(window, False)
    assert window._progress.stopped == 1
    assert window._progress.packed is False


def test_candidate_row_values_marks_the_current_video():
    from mvsync.gui import candidate_row_values
    from mvsync.mv_search import Candidate

    candidate = Candidate("v1", "Title", 200.0, "Chan")
    assert candidate_row_values(1, candidate, "v1") == ("1", "Title", "Chan", "3:20", "Current")
    assert candidate_row_values(1, candidate, None) == ("1", "Title", "Chan", "3:20", "")


def test_cli_parses_the_fix_command():
    from mvsync.cli import _build_parser

    assert _build_parser().parse_args(["fix"]).command == "fix"


def test_cli_parses_the_gui_and_menu_commands():
    from mvsync.cli import _build_parser

    assert _build_parser().parse_args(["gui"]).command == "gui"
    assert _build_parser().parse_args(["menu"]).command == "menu"


def test_no_arguments_launches_the_gui(monkeypatch):
    from mvsync import cli, gui

    calls = []
    monkeypatch.setattr(gui, "run_gui", lambda *a, **k: calls.append(True) or 0)
    assert cli.main([]) == 0
    assert calls == [True]


def test_the_gui_command_launches_the_gui(monkeypatch):
    from mvsync import cli, gui

    calls = []
    monkeypatch.setattr(gui, "run_gui", lambda *a, **k: calls.append(True) or 0)
    assert cli.main(["gui"]) == 0
    assert calls == [True]


def test_the_menu_command_still_opens_the_console_menu(monkeypatch):
    from mvsync import cli, gui

    monkeypatch.setattr(gui, "run_gui", lambda *a, **k: pytest.fail("gui must not open"))
    monkeypatch.setattr(cli, "menu", lambda: 0)
    assert cli.main(["menu"]) == 0


def _interrupt(*args, **kwargs):
    raise KeyboardInterrupt


@pytest.mark.parametrize("argv", [[], ["gui"]])
def test_ctrl_c_in_the_window_returns_130_not_a_traceback(argv, monkeypatch):
    """Both ways into the window sit inside the same try, so Ctrl+C never escapes."""
    from mvsync import cli, gui

    monkeypatch.setattr(gui, "run_gui", _interrupt)
    assert cli.main(argv) == 130


# cli.py's docstring promises the heavy imports stay lazy so --help stays fast. The
# probe runs in a fresh interpreter, because this pytest process has already
# imported most of these for other tests. It only imports and reports; the guard
# keeps anything the probe touches from opening a window.
LAZY_IMPORT_PROBE = """
import json
import sys

import mvsync.cli

mvsync.cli.main = lambda *a, **k: None

watched = [
    "winsdk",
    "soundcard",
    "librosa",
    "tkinter",
    "mvsync.gui",
    "mvsync.sync_runtime",
    "mvsync.scan_runtime",
    "mvsync.capture",
    "mvsync.player_smtc",
]
loaded = [
    name
    for name in sorted(sys.modules)
    if any(name == root or name.startswith(root + ".") for root in watched)
]
print(json.dumps(loaded))
"""


def test_importing_the_cli_pulls_in_no_heavy_module():
    from mvsync import paths

    env = dict(os.environ)
    env["PYTHONPATH"] = str(paths.src_dir())
    finished = subprocess.run(
        [sys.executable, "-c", LAZY_IMPORT_PROBE],
        capture_output=True,
        text=True,
        cwd=paths.project_root(),
        env=env,
        timeout=60,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr
    # Last line is the probe's own print, whatever else an import may have said.
    assert json.loads(finished.stdout.strip().splitlines()[-1]) == []


def test_main_window_actions_map_to_the_documented_commands():
    from mvsync.gui import MAIN_ACTIONS

    assert MAIN_ACTIONS == ("play", "scan", "fix", "doctor")


def test_log_lines_are_appended_in_order():
    from mvsync.gui import append_log

    widget = FakeText()
    append_log(widget, ["first\n", "second\n"])
    assert widget.value == "first\nsecond\n"


def test_append_log_leaves_the_pane_read_only():
    from mvsync.gui import append_log

    widget = FakeText()
    append_log(widget, ["only\n"])
    assert widget.state == "disabled"


def test_run_gui_takes_the_root_positionally_or_by_keyword():
    import inspect

    from mvsync.gui import run_gui

    parameter = inspect.signature(run_gui).parameters["root"]
    assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameter.default is None


# Every hotkey the play loop consumes, and the physical key it comes from. A typo
# here (say "bracketleft": "{") would quietly kill an offset trim on the user's
# keyboard, so the mapping is pinned rather than derived.
HOTKEY_CASES = [
    ("q", "q"),
    ("r", "r"),
    ("bracketleft", "["),
    ("bracketright", "]"),
    ("p", "p"),
]


def test_each_hotkey_keysym_pushes_the_key_the_play_loop_consumes():
    from mvsync.gui import HOTKEYS_BY_KEYSYM, MainWindow

    assert sorted(HOTKEYS_BY_KEYSYM) == sorted(keysym for keysym, _ in HOTKEY_CASES)
    for keysym, key in HOTKEY_CASES:
        window = StandInWindow()
        assert MainWindow._on_key(window, FakeEvent(keysym)) == "break"
        assert window._keys.poll() == key, keysym


def test_every_pushed_hotkey_is_one_the_play_loop_documents():
    from mvsync import sync_runtime
    from mvsync.gui import HOTKEYS_BY_KEYSYM

    for key in HOTKEYS_BY_KEYSYM.values():
        assert key in sync_runtime.HOTKEYS, key


def test_an_unknown_keysym_is_ignored():
    from mvsync.gui import MainWindow

    window = StandInWindow()
    assert MainWindow._on_key(window, FakeEvent("x")) is None
    assert window._keys.poll() == ""


def test_a_key_typed_into_the_url_entry_is_not_a_hotkey():
    from mvsync.gui import MainWindow

    window = StandInWindow()
    for keysym, _ in HOTKEY_CASES:
        event = FakeEvent(keysym, widget=window.url_entry)
        assert MainWindow._on_key(window, event) is None, keysym
    assert window._keys.poll() == ""


def test_a_key_from_any_other_widget_is_still_a_hotkey():
    from mvsync.gui import MainWindow

    window = StandInWindow()
    event = FakeEvent("q", widget=window.other_widget)
    assert MainWindow._on_key(window, event) == "break"
    assert window._keys.poll() == "q"


def test_the_log_pump_shows_worker_output_while_the_operation_runs():
    from mvsync.gui import LOG_PUMP_MS, MainWindow

    window = pump_stand_in()
    window._sink.write("[play] one\n")
    MainWindow._pump(window)
    assert window._log_text.value == "[play] one\n"
    assert window.scheduled == [(LOG_PUMP_MS, window._pump)]
    assert window._pump_after == "after#1"


def test_the_pump_and_the_final_drain_never_double_print_or_drop_lines():
    from mvsync.gui import MainWindow, append_log, drain_lines

    window = pump_stand_in()
    window._sink.write("[play] one\n")
    window._sink.write("[play] two")
    MainWindow._pump(window)
    assert window._log_text.value == "[play] one\n"

    window._sink.write(" more\n")
    append_log(window._log_text, drain_lines(window._sink))
    MainWindow._pump(window)
    assert window._log_text.value == "[play] one\n[play] two more\n"
    assert drain_lines(window._sink) == []


def test_the_pump_stops_when_the_window_is_closing():
    from mvsync.gui import MainWindow

    window = pump_stand_in(closing=True)
    window._sink.write("[play] one\n")
    MainWindow._pump(window)
    assert window._log_text.value == ""
    assert window.scheduled == []


def test_draining_without_a_sink_yields_no_lines():
    from mvsync.gui import drain_lines

    assert drain_lines(None) == []


def test_scan_candidates_uses_the_url_read_on_the_tk_thread():
    from mvsync.gui import MainWindow

    window = StandInWindow(state=None)
    candidates = MainWindow._scan_candidates(window, "https://youtu.be/abcdefghijk")
    assert [c.video_id for c in candidates] == ["abcdefghijk"]


def test_scan_candidates_rejects_an_unparsable_url():
    from mvsync.gui import MainWindow

    window = StandInWindow(state=None)
    with pytest.raises(ValueError):
        MainWindow._scan_candidates(window, "not a url")


def test_scan_candidates_without_a_url_or_a_track_raises():
    from mvsync.gui import MainWindow

    window = StandInWindow(state=None)
    with pytest.raises(RuntimeError):
        MainWindow._scan_candidates(window, "")


def test_enabled_actions_lock_out_everything_while_a_session_holds_the_lock():
    from mvsync.gui import enabled_actions

    assert enabled_actions(busy=False, locked=True) == ()


def test_enabled_actions_allow_play_when_idle_and_unlocked():
    from mvsync.gui import enabled_actions

    assert enabled_actions(busy=False, locked=False) == MAIN_ACTIONS


def test_enabled_actions_disable_everything_while_busy():
    from mvsync.gui import enabled_actions

    assert enabled_actions(busy=True, locked=False) == ()


def test_enabled_actions_require_a_track_except_for_setup():
    from mvsync.gui import enabled_actions

    assert enabled_actions(busy=False, locked=False, has_track=False) == ("doctor",)


def test_the_buttons_go_disabled_while_another_session_holds_the_lock(tmp_path, monkeypatch):
    from mvsync import session

    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)

    assert window._refresh_lock() is True
    assert window._locked is True
    assert all(button.state == "disabled" for button in window._buttons.values())
    assert "another session is already running (pid 424242)" in window._log_text.value


def test_the_lock_notice_is_logged_once_not_on_every_refresh(tmp_path, monkeypatch):
    from mvsync import session

    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)
    window._refresh_lock()
    window._log_text.value = ""

    window._refresh_lock()
    assert window._log_text.value == ""


def test_the_buttons_come_back_when_the_other_session_ends(tmp_path, monkeypatch):
    from mvsync import session
    from mvsync.player_smtc import PlayerState

    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)
    window._state = PlayerState("Artist", "Song", 180.0, 30.0, 100.0, True)
    window._refresh_lock()

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    assert window._refresh_lock() is False
    assert all(button.state == "normal" for button in window._buttons.values())


def test_the_play_button_stays_enabled_as_stop_while_its_own_session_runs(tmp_path, monkeypatch):
    from mvsync import session

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    window = lock_stand_in(tmp_path)
    window._busy = True
    window._action = "play"

    window._refresh_lock()
    assert window._buttons["play"].state == "normal"
    assert window._buttons["scan"].state == "disabled"


def test_the_play_worker_passes_the_stop_event(tmp_path, monkeypatch):
    import queue

    from mvsync import gui, sync_runtime

    window = StandInWindow()
    window._root_dir = tmp_path
    window._play_stop = threading.Event()
    window._video_switches = queue.Queue()
    captured = {}
    monkeypatch.setattr(
        sync_runtime, "run_play", lambda *args, **kwargs: captured.update(kwargs) or 0
    )

    assert gui.MainWindow._play_worker(window) == 0
    assert captured["stop_event"] is window._play_stop
    assert captured["switches"] is window._video_switches


def test_stopping_play_sets_the_stop_event_before_joining():
    from mvsync.gui import MainWindow

    observed = []

    class Worker:
        def join(self, timeout=None):
            observed.append(window._play_stop.is_set())

    window = StandInWindow()
    window._play_stop = threading.Event()
    window._worker = Worker()

    MainWindow._stop_play(window)

    assert window._play_stop.is_set()
    assert observed == [True]


def test_starting_play_gets_a_fresh_stop_event():
    from mvsync.gui import MainWindow

    window = StandInWindow()
    window._action = None
    window._play_stop = threading.Event()
    window._play_stop.set()
    window._play_worker = lambda: 0
    window._reset_keys = lambda: None
    window._run = lambda action, worker: None

    MainWindow.play(window)

    assert not window._play_stop.is_set()


def test_a_locked_window_starts_no_worker_and_says_why(tmp_path, monkeypatch):
    from mvsync import session
    from mvsync.gui import MainWindow

    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)
    started = []

    MainWindow._run(window, "play", lambda: started.append("ran") or 0)
    assert started == []
    assert "another session is already running (pid 424242)" in window._log_text.value


def test_a_refusal_points_at_refresh_track_as_the_way_back(tmp_path, monkeypatch):
    """The lock is only re-read on demand, so the user needs to be told what re-reads it."""
    from mvsync import session
    from mvsync.gui import MainWindow

    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)

    MainWindow._run(window, "play", lambda: 0)
    assert "Refresh" in window._log_text.value
    assert window._status_label.options["text"] == "Another session is running"
    assert window._busy is False


def test_the_status_line_says_a_session_holds_the_lock(tmp_path, monkeypatch):
    from mvsync import session

    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)

    window._refresh_lock()
    assert window._status_label.options["text"] == "Another session is running"


def test_the_status_line_returns_to_ready_when_the_other_session_ends(tmp_path, monkeypatch):
    from mvsync import session

    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)
    window._refresh_lock()

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    window._refresh_lock()
    assert window._status_label.options["text"] == "Ready"


def test_the_status_line_names_the_operation_that_is_running(tmp_path, monkeypatch):
    from mvsync import session

    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)
    window._busy = True
    window._action = "play"

    window._refresh_lock()
    assert window._status_label.options["text"] == "Playing"


def test_lock_status_names_the_pid_and_the_way_out():
    from mvsync.gui import lock_status

    text = lock_status(424242)
    assert "424242" in text
    assert "Refresh" in text


def test_refreshing_the_track_re_reads_the_lock(tmp_path, monkeypatch):
    from mvsync import gui, session

    monkeypatch.setattr(gui, "PlayerBridge", FakeBridge)
    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)

    gui.MainWindow.refresh_track(window)
    assert window._locked is True
    assert all(button.state == "disabled" for button in window._buttons.values())


def test_a_finished_worker_re_reads_the_lock(tmp_path, monkeypatch):
    from mvsync import session
    from mvsync.gui import MainWindow

    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)
    window._busy = True
    window._action = "play"
    window._sink.write("[play] done\n")

    MainWindow._run_finished(window, "play", 0)
    assert window._busy is False
    assert window._locked is True
    assert all(button.state == "disabled" for button in window._buttons.values())


# The success path of _run: the worker thread, the stdout redirect into the sink,
# the log pump, and the after() that hands the result to _run_finished. It used to
# have no coverage at all, and every IMPORTANT finding in this file was found by
# hand rather than by the suite, so the net matters.
def test_run_starts_a_worker_and_captures_its_stdout(tmp_path, monkeypatch):
    from mvsync import session
    from mvsync.gui import LOG_PUMP_MS, MainWindow, drain_lines

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    window = run_stand_in(tmp_path)

    def work():
        print("[play] downloading")
        return 0

    MainWindow._run(window, "play", work)
    assert window._busy is True
    assert window._status_label.options["text"] == "Playing"
    assert window._hotkey_row.options["packed"] is True
    assert (LOG_PUMP_MS, window._pump) in window.scheduled

    join_worker(window)
    assert drain_lines(window._sink) == ["[play] downloading\n"]


def test_run_hands_the_result_to_run_finished_on_the_tk_thread(tmp_path, monkeypatch):
    from mvsync import session
    from mvsync.gui import MainWindow

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    window = run_stand_in(tmp_path)

    MainWindow._run(window, "doctor", lambda: 3)
    join_worker(window)

    assert (0, window._run_finished, "doctor", 3) in window.scheduled


def test_run_does_not_schedule_a_finish_once_the_window_is_closing(tmp_path, monkeypatch):
    from mvsync import session
    from mvsync.gui import MainWindow

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    window = run_stand_in(tmp_path)
    gate = threading.Event()

    def work():
        gate.wait(timeout=5.0)
        return 0

    MainWindow._run(window, "doctor", work)
    window._closing = True
    gate.set()
    join_worker(window)

    assert not any(call[1] is window._run_finished for call in window.scheduled)


def test_a_worker_that_raises_becomes_exit_code_one_instead_of_a_traceback(
    tmp_path, monkeypatch
):
    from mvsync import session
    from mvsync.gui import MainWindow, drain_lines

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    window = run_stand_in(tmp_path)

    def work():
        print("[scan] listening")
        raise RuntimeError("no device")

    MainWindow._run(window, "scan", work)
    join_worker(window)

    assert (0, window._run_finished, "scan", 1) in window.scheduled
    assert "[gui] scan failed: no device" in "".join(drain_lines(window._sink))


def test_run_refuses_a_second_operation_while_one_is_already_running(tmp_path, monkeypatch):
    from mvsync import session
    from mvsync.gui import MainWindow

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    window = run_stand_in(tmp_path)
    window._busy = True
    started = []

    MainWindow._run(window, "play", lambda: started.append("ran") or 0)
    assert started == []
    assert "another operation is already running" in window._log_text.value


def test_run_finished_puts_the_window_back_the_way_it_was(tmp_path, monkeypatch):
    from mvsync import session
    from mvsync.gui import MainWindow
    from mvsync.player_smtc import PlayerState

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    window = run_stand_in(tmp_path)
    window._state = PlayerState("Artist", "Song", 180.0, 30.0, 100.0, True)
    window._busy = True
    window._action = "play"
    window._pump_after = "after#1"
    window.scheduled.append((250, window._pump))
    window._sink.write("[play] last words\n")

    MainWindow._run_finished(window, "play", 0)

    assert window._busy is False
    assert window._action is None
    assert window._pump_after is None
    assert window.cancelled == ["after#1"]
    assert window._hotkey_row.options["packed"] is False
    assert window._status_label.options["text"] == "Ready"
    assert all(button.state == "normal" for button in window._buttons.values())
    assert "[play] last words\n" in window._log_text.value
    assert "exited with code" not in window._log_text.value


def test_run_finished_reports_a_nonzero_exit_code(tmp_path, monkeypatch):
    from mvsync import session
    from mvsync.gui import MainWindow

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    window = run_stand_in(tmp_path)

    MainWindow._run_finished(window, "doctor", 1)
    assert "[gui] doctor exited with code 1" in window._log_text.value
    assert "[gui] doctor finished" in window._log_text.value


def test_run_finished_refreshes_the_track_after_a_scan(tmp_path, monkeypatch):
    from mvsync import session
    from mvsync.gui import MainWindow

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    refreshed = []
    window = run_stand_in(tmp_path)
    window.refresh_track = lambda: refreshed.append(True)

    MainWindow._run_finished(window, "scan", 0)
    assert refreshed == [True]


def test_use_url_reads_on_the_tk_thread_and_the_worker_gets_a_string(
    tmp_path, monkeypatch
):
    """Tk raises RuntimeError for a StringVar touched off its own thread."""
    from pathlib import Path

    from mvsync import gui, session
    from mvsync.gui import MainWindow
    from mvsync.mv_search import Candidate
    from mvsync.scan_runtime import ScanResult

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    monkeypatch.setattr(
        gui,
        "scan_with_candidates",
        lambda candidates, root=None, on_progress=None: ScanResult(
            path=Path("map.json"), video_id=candidates[0].video_id
        ),
    )
    window = run_stand_in(tmp_path)
    window._url_var = ThreadCheckedUrlVar("https://youtu.be/abcdefghijk")
    window._scan_candidates = lambda url: [Candidate("abcdefghijk", "t", 180.0, "c")]
    window.use_url = partial(MainWindow.use_url, window)
    window._start_scan = partial(MainWindow._start_scan, window)
    window._scan_worker = partial(MainWindow._scan_worker, window)
    started = []
    window._run = lambda action, worker: started.append((action, worker))

    window.use_url()
    action, worker = started.pop()

    results = []
    thread = threading.Thread(target=lambda: results.append(worker()))
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert action == "scan"
    assert results == [0]
    assert window._url_var.reads == [threading.current_thread()]


def test_the_close_prompt_only_promises_to_stop_a_running_play():
    from mvsync.gui import close_question

    assert "stops it" in close_question("play")
    assert "stops it" not in close_question("scan")
    assert "abandons" in close_question("scan")
    assert "stops it" not in close_question(None)


class StandInFixer:
    """Only the attributes the fixer methods under test read."""

    def __init__(self):
        self.busy = True
        self.refreshed = 0
        self.back_calls = 0
        self.closed = False

    def _set_busy(self, value):
        self.busy = value

    def refresh_track(self):
        self.refreshed += 1

    def back(self):
        self.back_calls += 1

    def _log(self, message):
        pass

    def _on_close(self):
        self.closed = True


def test_the_fix_window_back_uses_the_close_confirmation():
    from mvsync.gui import FixerWindow

    window = StandInFixer()
    FixerWindow.back(window)
    assert window.closed is True


def test_a_successful_rebuild_closes_the_window():
    from pathlib import Path

    from mvsync import gui
    from mvsync.scan_runtime import ScanResult

    window = StandInFixer()

    gui.FixerWindow._use_done(window, ScanResult(path=Path("m.json"), video_id="v"))

    assert window.busy is False
    assert window.back_calls == 1
    assert window.refreshed == 1


class StandInLinkVar:
    """Stand-in for a tk StringVar: get and set never touch a Tk root."""

    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def link_fixer(text):
    from mvsync.gui import FixerWindow

    window = StandInFixer()
    window._link_var = StandInLinkVar(text)
    window._status_label = FakeWidget()
    window._state = None
    window.rebuilt = []
    window._rebuild = lambda candidates: window.rebuilt.append(candidates)
    return window, FixerWindow


def test_use_link_rebuilds_with_the_pasted_video():
    window, fixer = link_fixer("https://www.youtube.com/watch?v=Mmw-PpWhKFg")

    fixer.use_link(window)

    assert [[candidate.video_id for candidate in group] for group in window.rebuilt] == [
        ["Mmw-PpWhKFg"]
    ]


def test_use_link_rebuilds_from_a_bare_video_id():
    window, fixer = link_fixer("Mmw-PpWhKFg")

    fixer.use_link(window)

    assert [[candidate.video_id for candidate in group] for group in window.rebuilt] == [
        ["Mmw-PpWhKFg"]
    ]


def test_use_link_rejects_text_that_is_not_a_link():
    window, fixer = link_fixer("not a link")

    fixer.use_link(window)

    assert window.rebuilt == []
    assert "link" in window._status_label.options["text"]


def test_use_link_without_text_asks_for_one():
    window, fixer = link_fixer("   ")

    fixer.use_link(window)

    assert window.rebuilt == []
    assert "Paste" in window._status_label.options["text"]


def test_a_track_change_clears_a_pending_link(tmp_path, monkeypatch):
    from mvsync import gui

    window = StandInFixer()
    window._state = None
    window._track_id = "old"
    window._root_dir = tmp_path
    window._candidates = []
    window._in_use_video_id = None
    window._link_var = StandInLinkVar("https://youtu.be/abcdefghijk")
    window._track_label = FakeWidget()
    window._status_label = FakeWidget()
    window._fill_table = lambda: None
    window._update_buttons = lambda: None
    monkeypatch.setattr(gui, "PlayerBridge", FakeBridge)

    gui.FixerWindow.refresh_track(window)

    assert window._link_var.get() == ""


def picker_link_window(tmp_path, text):
    import queue

    from mvsync.align_store import make_track_id
    from mvsync.player_smtc import PlayerState

    state = PlayerState("Artist", "Song", 180.0, 30.0, 100.0, True)
    track_id = make_track_id(state.artist, state.title)
    window = StandInWindow(state=state)
    window._root_dir = tmp_path
    window._picker_open = True
    window._picker_track_id = track_id
    window._picker_candidates = []
    window._picker_current_video = None
    window._picker_link_var = StandInLinkVar(text)
    window._action = "play"
    window._video_switches = queue.Queue()
    window.closed = []
    window.notices = []
    window._close_picker = lambda: window.closed.append(True)
    window._show_notice = lambda message, kind="danger": window.notices.append(message)
    return window, track_id


def test_picker_use_link_saves_the_choice_and_queues_a_switch(tmp_path):
    from mvsync import gui, video_choice

    window, track_id = picker_link_window(
        tmp_path, "https://www.youtube.com/watch?v=Mmw-PpWhKFg"
    )

    gui.MainWindow.use_link(window)

    assert video_choice.load_video_choice(track_id, tmp_path) == "Mmw-PpWhKFg"
    switch = window._video_switches.get_nowait()
    assert (switch.track_id, switch.video_id) == (track_id, "Mmw-PpWhKFg")
    assert window.closed == [True]


def test_picker_use_link_rejects_text_that_is_not_a_link(tmp_path):
    from mvsync import gui, video_choice

    window, track_id = picker_link_window(tmp_path, "not a link")

    gui.MainWindow.use_link(window)

    assert video_choice.load_video_choice(track_id, tmp_path) is None
    assert window.closed == []
    assert window.notices and "link" in window.notices[0]


def test_picker_use_link_without_text_asks_for_one(tmp_path):
    from mvsync import gui, video_choice

    window, track_id = picker_link_window(tmp_path, "   ")

    gui.MainWindow.use_link(window)

    assert video_choice.load_video_choice(track_id, tmp_path) is None
    assert window.notices and "Paste" in window.notices[0]


def test_opening_the_picker_clears_a_stale_link(tmp_path):
    from mvsync import gui

    window, _ = picker_link_window(tmp_path, "https://youtu.be/abcdefghijk")
    window._picker_open = False
    window._picker_search_button = FakeWidget()
    window._picker_track_label = FakeWidget()
    window._picker_status = FakeWidget()
    window._fill_picker_table = lambda: None
    window._update_picker_buttons = lambda: None
    window._show_picker_view = lambda: None

    gui.MainWindow.open_picker(window)

    assert window._picker_link_var.get() == ""


def test_a_picker_track_change_clears_a_pending_link(tmp_path):
    from mvsync import gui
    from mvsync.player_smtc import PlayerState

    window, _ = picker_link_window(tmp_path, "https://youtu.be/abcdefghijk")
    window._state = PlayerState("Artist", "Other", 180.0, 30.0, 100.0, True)
    window._picker_search_button = FakeWidget()
    window._picker_track_label = FakeWidget()
    window._picker_status = FakeWidget()
    window._fill_picker_table = lambda: None
    window._update_picker_buttons = lambda: None

    gui.MainWindow._reset_picker_track(window)

    assert window._picker_link_var.get() == ""


def test_player_index_prefers_the_configured_match():
    from mvsync.gui import player_index
    from mvsync.player_smtc import PlayerSession

    sessions = [
        PlayerSession("browser.exe", "Video", "Channel", True),
        PlayerSession("vlc.exe", "Song", "Artist", False),
    ]
    assert player_index(sessions, "vlc") == 1
    assert player_index(sessions, "missing") is None
    assert player_index([], "vlc") is None


def test_player_index_without_a_match_uses_the_first_playing_session():
    from mvsync.gui import player_index
    from mvsync.player_smtc import PlayerSession

    sessions = [
        PlayerSession("browser.exe", "Video", "Channel", False),
        PlayerSession("vlc.exe", "Song", "Artist", True),
    ]
    assert player_index(sessions, "") == 1


def test_player_index_without_a_match_or_a_playing_session_uses_the_first():
    from mvsync.gui import player_index
    from mvsync.player_smtc import PlayerSession

    sessions = [
        PlayerSession("browser.exe", "Video", "Channel", False),
        PlayerSession("vlc.exe", "Song", "Artist", False),
    ]
    assert player_index(sessions, "") == 0


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
    monkeypatch.setattr(
        gui.config, "save_player", lambda app, exe, root=None: saved.append((app, exe))
    )

    gui.MainWindow._select_player(window)

    assert saved == [("vlc.exe", "vlc.exe")]


def test_choose_video_is_enabled_while_playing(tmp_path, monkeypatch):
    from mvsync import session

    monkeypatch.setattr(session, "lock_holder", lambda root: 0)
    window = lock_stand_in(tmp_path)
    window._busy = True
    window._action = "play"

    window._refresh_lock()

    assert window._buttons["play"].state == "normal"
    assert window._buttons["fix"].state == "normal"
    assert window._buttons["scan"].state == "disabled"
    assert window._buttons["doctor"].state == "disabled"


def test_scan_uses_the_current_track_and_does_not_read_the_url():
    from mvsync.gui import MainWindow

    window = StandInWindow(state=object())
    window._url_var = ThreadCheckedUrlVar("https://youtu.be/abcdefghijk")
    started = []
    window._start_scan = lambda url: started.append(url)

    MainWindow.scan(window)

    assert started == [""]
    assert window._url_var.reads == []


def test_use_url_reads_the_entry_on_the_tk_thread():
    from mvsync.gui import MainWindow

    url = "https://youtu.be/abcdefghijk"
    window = StandInWindow()
    window._url_var = ThreadCheckedUrlVar(url)
    started = []
    window._start_scan = lambda value: started.append(value)

    MainWindow.use_url(window)

    assert started == [url]
    assert window._url_var.reads == [threading.current_thread()]


def test_use_video_saves_the_choice_and_queues_a_switch(tmp_path):
    import queue

    from mvsync import gui, video_choice
    from mvsync.align_store import make_track_id
    from mvsync.mv_search import Candidate
    from mvsync.player_smtc import PlayerState

    state = PlayerState("Artist", "Song", 180.0, 30.0, 100.0, True)
    track_id = make_track_id(state.artist, state.title)
    window = StandInWindow(state=state)
    window._root_dir = tmp_path
    window._picker_open = True
    window._picker_track_id = track_id
    window._picker_candidates = [Candidate("abcdefghijk", "Title", 180.0, "Channel")]
    window._picker_current_video = None
    window._picker_tree = type("Tree", (), {"selection": lambda self: ("1",)})()
    window._action = "play"
    window._video_switches = queue.Queue()
    closed, notices = [], []
    window._close_picker = lambda: closed.append(True)
    window._show_notice = lambda message, kind="danger": notices.append(message)

    gui.MainWindow._use_selected(window)

    assert video_choice.load_video_choice(track_id, tmp_path) == "abcdefghijk"
    switch = window._video_switches.get_nowait()
    assert switch.track_id == track_id
    assert switch.video_id == "abcdefghijk"
    assert closed == [True]
    assert notices and "refined" in notices[0]


def test_use_video_refuses_after_a_track_change(tmp_path):
    import queue

    from mvsync import gui, video_choice
    from mvsync.align_store import make_track_id
    from mvsync.mv_search import Candidate
    from mvsync.player_smtc import PlayerState

    state = PlayerState("Artist", "Other", 180.0, 30.0, 100.0, True)
    window = StandInWindow(state=state)
    window._root_dir = tmp_path
    window._picker_open = True
    window._picker_track_id = make_track_id("Artist", "Song")
    window._picker_candidates = [Candidate("abcdefghijk", "Title", 180.0, "Channel")]
    window._picker_current_video = None
    window._picker_tree = type("Tree", (), {"selection": lambda self: ("1",)})()
    window._action = "play"
    window._video_switches = queue.Queue()
    notices = []
    window._close_picker = lambda: None
    window._show_notice = lambda message, kind="danger": notices.append(message)

    gui.MainWindow._use_selected(window)

    assert video_choice.load_video_choice(window._picker_track_id, tmp_path) is None
    assert window._video_switches.empty()
    assert notices and "track changed" in notices[0]


def test_use_video_does_not_queue_a_switch_when_stopped(tmp_path):
    import queue

    from mvsync import gui, video_choice
    from mvsync.align_store import make_track_id
    from mvsync.mv_search import Candidate
    from mvsync.player_smtc import PlayerState

    state = PlayerState("Artist", "Song", 180.0, 30.0, 100.0, True)
    track_id = make_track_id(state.artist, state.title)
    window = StandInWindow(state=state)
    window._root_dir = tmp_path
    window._picker_open = True
    window._picker_track_id = track_id
    window._picker_candidates = [Candidate("abcdefghijk", "Title", 180.0, "Channel")]
    window._picker_current_video = None
    window._picker_tree = type("Tree", (), {"selection": lambda self: ("1",)})()
    window._action = None
    window._video_switches = queue.Queue()
    window._close_picker = lambda: None
    window._show_notice = lambda message, kind="danger": None

    gui.MainWindow._use_selected(window)

    assert video_choice.load_video_choice(track_id, tmp_path) == "abcdefghijk"
    assert window._video_switches.empty()


def test_sync_badge_describes_rough_and_ready(tmp_path):
    from mvsync import paths, video_choice
    from mvsync.align_store import make_track_id, save_alignment
    from mvsync.gui import sync_badge_text
    from mvsync.timeline import Alignment, Anchor

    track_id = make_track_id("Artist", "Song")
    assert sync_badge_text(track_id, tmp_path) == ("First listen, refining", "rough")

    video_choice.save_video_choice(track_id, "video", tmp_path)
    assert sync_badge_text(track_id, tmp_path) == ("Rough, refining", "rough")

    alignment = Alignment(
        video_id="video",
        track_id=track_id,
        video_duration_s=200.0,
        track_duration_s=180.0,
        anchors=[Anchor(0.0, 0.0), Anchor(180.0, 180.0)],
    )
    save_alignment(paths.ensure_dir(paths.alignments_dir(tmp_path)), alignment)
    assert sync_badge_text(track_id, tmp_path) == ("Synced", "synced")

    video_choice.save_video_choice(track_id, "other", tmp_path)
    assert sync_badge_text(track_id, tmp_path) == ("Rough, refining", "rough")


def test_auto_refresh_reschedules_itself():
    from mvsync.gui import TRACK_REFRESH_MS, MainWindow

    window = StandInWindow()
    window._busy = False
    calls = []
    window.refresh_track = lambda quiet=False: calls.append(quiet)

    MainWindow._auto_refresh(window)

    assert calls == [True]
    assert window._refresh_after == "after#1"
    assert window.scheduled == [(TRACK_REFRESH_MS, window._auto_refresh)]


def test_show_notice_sets_an_inline_message():
    from mvsync.gui import MainWindow

    window = StandInWindow()
    window._notice = FakeWidget()
    window._notice_label = FakeWidget()
    window._notice_kind = None

    MainWindow._show_notice(window, "No player found", kind="danger")
    assert window._notice_label.options["text"] == "No player found"
    assert window._notice.options["packed"] is True
    assert window._notice_kind == "danger"

    MainWindow._clear_notice(window, kind="lock")
    assert window._notice.options["packed"] is True

    MainWindow._clear_notice(window, kind="danger")
    assert window._notice.options["packed"] is False


def test_toggle_url_reveals_the_entry_and_returns_it():
    from mvsync.gui import MainWindow

    window = StandInWindow()
    window._url_panel = FakeWidget()
    window._url_toggle = FakeWidget()
    window._url_entry = FakeWidget()
    window._url_revealed = False

    MainWindow._toggle_url(window)
    assert window._url_revealed is True
    assert window._url_panel.options["packed"] is True
    assert window._url_entry.options["focused"] is True
    assert window._url_toggle.options["text"] == "Hide video link"

    MainWindow._toggle_url(window)
    assert window._url_revealed is False
    assert window._url_panel.options["packed"] is False


def test_closing_the_picker_clears_a_pending_search():
    from mvsync.gui import MainWindow

    window = StandInWindow()
    window._picker_open = True
    window._picker_busy = True
    window._show_now_view = lambda: None
    window._update_buttons = lambda: None
    window._update_status = lambda: None

    MainWindow._close_picker(window)

    assert window._picker_open is False
    assert window._picker_busy is False


def test_a_finished_search_releases_the_picker_even_after_it_closed():
    from mvsync.gui import MainWindow

    window = StandInWindow()
    window._picker_open = False
    window._picker_track_id = "track"
    window._picker_busy = True
    window._picker_search_button = FakeWidget()

    MainWindow._picker_search_done(window, "track", [])

    assert window._picker_busy is False
    assert window._picker_search_button.options["state"] == "normal"


def test_sync_badge_uses_the_choice_map_over_a_newer_other_map(tmp_path):
    import time

    from mvsync import paths, video_choice
    from mvsync.align_store import make_track_id, save_alignment
    from mvsync.gui import sync_badge_text
    from mvsync.timeline import Alignment, Anchor

    track_id = make_track_id("Artist", "Song")
    store = paths.ensure_dir(paths.alignments_dir(tmp_path))
    chosen = Alignment(
        video_id="chosen",
        track_id=track_id,
        video_duration_s=200.0,
        track_duration_s=180.0,
        anchors=[Anchor(0.0, 0.0), Anchor(180.0, 180.0)],
    )
    other = Alignment(
        video_id="other",
        track_id=track_id,
        video_duration_s=200.0,
        track_duration_s=180.0,
        anchors=[Anchor(0.0, 0.0), Anchor(180.0, 180.0)],
    )
    save_alignment(store, chosen)
    time.sleep(0.01)
    save_alignment(store, other)
    video_choice.save_video_choice(track_id, "chosen", tmp_path)

    assert sync_badge_text(track_id, tmp_path) == ("Synced", "synced")


def test_player_controls_are_disabled_while_a_worker_runs(tmp_path):
    from mvsync.gui import MainWindow
    from mvsync.player_smtc import PlayerState

    window = lock_stand_in(tmp_path)
    window._state = PlayerState("Artist", "Song", 180.0, 30.0, 100.0, True)
    window._player_box = FakeWidget()
    window._refresh_button = FakeWidget()
    window._busy = True
    window._action = "play"

    MainWindow._update_buttons(window)
    assert window._player_box.options["state"] == "disabled"
    assert window._refresh_button.options["state"] == "disabled"

    window._busy = False
    window._action = None
    MainWindow._update_buttons(window)
    assert window._player_box.options["state"] == "readonly"
    assert window._refresh_button.options["state"] == "normal"


def test_use_video_rechecks_the_track_before_saving(tmp_path):
    import queue

    from mvsync import gui, video_choice
    from mvsync.align_store import make_track_id
    from mvsync.mv_search import Candidate
    from mvsync.player_smtc import PlayerState

    old_state = PlayerState("Artist", "Song", 180.0, 30.0, 100.0, True)
    new_state = PlayerState("Artist", "Next", 180.0, 30.0, 100.0, True)
    track_id = make_track_id(old_state.artist, old_state.title)
    window = StandInWindow(state=old_state)
    window._root_dir = tmp_path
    window._picker_open = True
    window._picker_track_id = track_id
    window._picker_candidates = [Candidate("abcdefghijk", "Title", 180.0, "Channel")]
    window._picker_current_video = None
    window._picker_tree = type("Tree", (), {"selection": lambda self: ("1",)})()
    window._action = "play"
    window._video_switches = queue.Queue()
    window._close_picker = lambda: None
    notices = []
    window._show_notice = lambda message, kind="danger": notices.append(message)
    window.refresh_track = lambda quiet=False: setattr(window, "_state", new_state)

    gui.MainWindow._use_selected(window)

    assert video_choice.load_video_choice(track_id, tmp_path) is None
    assert window._video_switches.empty()
    assert notices and "track changed" in notices[0]
