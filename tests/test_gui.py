import json
import os
import subprocess
import sys
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
        self.scheduled = []

    def after(self, delay_ms, callback):
        self.scheduled.append((delay_ms, callback))
        return f"after#{len(self.scheduled)}"

    def after_cancel(self, handle):
        self.scheduled.remove(handle)


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
    window._root_dir = root
    window._buttons = {action: FakeButton() for action in MAIN_ACTIONS}
    window._track_label = FakeWidget()
    window._status_label = FakeWidget()
    window._hotkey_row = FakeWidget()
    window._log = partial(MainWindow._log, window)
    window._refresh_lock = partial(MainWindow._refresh_lock, window)
    window._update_buttons = partial(MainWindow._update_buttons, window)
    window._set_status = partial(MainWindow._set_status, window)
    window._set_hotkeys_visible = partial(MainWindow._set_hotkeys_visible, window)
    window._cancel_pump = partial(MainWindow._cancel_pump, window)
    return window


def test_gui_module_exposes_run_fixer():
    from mvsync import gui

    assert callable(gui.run_fixer)


def test_format_length():
    from mvsync.gui import format_length

    assert format_length(None) == "?"
    assert format_length(221.0) == "3:41"


def test_candidate_row_values_marks_the_one_in_use():
    from mvsync.gui import candidate_row_values
    from mvsync.mv_search import Candidate

    candidate = Candidate("v1", "Title", 200.0, "Chan")
    assert candidate_row_values(1, candidate, "v1") == ("1", "Title", "Chan", "3:20", "in use")
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
    "mvsync.spotify_smtc",
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

    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)
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


def test_a_locked_window_starts_no_worker_and_says_why(tmp_path, monkeypatch):
    from mvsync import session
    from mvsync.gui import MainWindow

    monkeypatch.setattr(session, "lock_holder", lambda root: 424242)
    window = lock_stand_in(tmp_path)
    started = []

    MainWindow._run(window, "play", lambda: started.append("ran") or 0)
    assert started == []
    assert "another session is already running (pid 424242)" in window._log_text.value


def test_refreshing_the_track_re_reads_the_lock(tmp_path, monkeypatch):
    from mvsync import gui, session

    monkeypatch.setattr(gui, "SpotifyBridge", FakeBridge)
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
