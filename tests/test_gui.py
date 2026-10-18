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


def test_main_window_actions_map_to_the_documented_commands():
    from mvsync.gui import MAIN_ACTIONS

    assert MAIN_ACTIONS == ("doctor", "scan", "fix", "play")


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
