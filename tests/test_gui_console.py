from mvsync.gui_console import KeySource, LogSink


def test_log_sink_collects_whole_lines():
    sink = LogSink()
    sink.write("hello\n")
    assert sink.drain() == ["hello"]


def test_log_sink_holds_back_a_line_without_a_newline():
    sink = LogSink()
    sink.write("par")
    assert sink.drain() == []
    sink.write("tial\n")
    assert sink.drain() == ["partial"]


def test_log_sink_write_returns_the_character_count():
    sink = LogSink()
    assert sink.write("abc\n") == 4


def test_log_sink_drops_the_oldest_lines_past_the_cap():
    """Drain hands back lines without their terminator, like the other drain cases."""
    sink = LogSink(max_lines=2)
    sink.write("a\n")
    sink.write("b\n")
    sink.write("c\n")
    assert sink.text == "b\nc\n"
    assert sink.drain() == ["b", "c"]


def test_log_sink_is_a_stdout_stub():
    sink = LogSink()
    assert sink.encoding == "utf-8"
    assert sink.errors == "replace"
    assert sink.isatty() is False
    assert sink.fileno() == -1


def test_key_source_returns_pushes_in_order_then_empty():
    keys = KeySource()
    assert keys.poll() == ""
    keys.push("r")
    keys.push("]")
    assert keys.poll() == "r"
    assert keys.poll() == "]"
    assert keys.poll() == ""
