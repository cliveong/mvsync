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
