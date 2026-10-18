from mvsync.gui_console import KeySource, LogSink


def test_log_sink_collects_whole_lines():
    sink = LogSink()
    sink.write("hello\n")
    assert sink.drain() == ["hello\n"]


def test_log_sink_holds_back_a_line_without_a_newline():
    sink = LogSink()
    sink.write("par")
    assert sink.drain() == []
    sink.write("tial\n")
    assert sink.drain() == ["partial\n"]


def test_log_sink_write_returns_the_character_count():
    sink = LogSink()
    assert sink.write("abc\n") == 4


def test_log_sink_splits_several_lines_from_one_write():
    sink = LogSink()
    sink.write("a\nb\n")
    assert sink.drain() == ["a\n", "b\n"]


def test_log_sink_drops_the_oldest_lines_past_the_cap():
    sink = LogSink(max_lines=2)
    sink.write("a\n")
    sink.write("b\n")
    sink.write("c\n")
    assert sink.text == "b\nc\n"
    assert sink.drain() == ["b\n", "c\n"]


def test_log_sink_cap_counts_only_newline_terminated_lines():
    """A \\r or other splitlines separator is content, not an extra line for the cap."""
    sink = LogSink(max_lines=2)
    sink.write("a\nb\rc\n")
    assert sink.text == "a\nb\rc\n"
    assert sink.drain() == ["a\n", "b\rc\n"]


def test_log_sink_cap_of_zero_and_one():
    empty = LogSink(max_lines=0)
    empty.write("a\nb\n")
    assert empty.text == ""
    one = LogSink(max_lines=1)
    one.write("a\nb\n")
    assert one.drain() == ["b\n"]


def test_log_sink_drain_keeps_a_partial_line_for_the_next_write():
    sink = LogSink()
    sink.write("done\nhalf")
    assert sink.drain() == ["done\n"]
    assert sink.text == "half"
    sink.write(" more\n")
    assert sink.drain() == ["half more\n"]


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


def test_concurrent_use_does_not_raise():
    import threading

    sink = LogSink(max_lines=50)
    keys = KeySource()

    def writer() -> None:
        for _ in range(100):
            sink.write("line\n")

    def reader() -> None:
        for _ in range(100):
            sink.drain()
            keys.push("q")

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert keys.poll() != "" or sink.text == ""


def test_log_sink_loses_no_writes_under_concurrency():
    import threading

    total = 10_000
    sink = LogSink(max_lines=total + 10)
    done = threading.Event()

    def writer() -> None:
        try:
            for index in range(total):
                sink.write(f"{index}\n")
        finally:
            done.set()

    drained: list[str] = []

    def reader() -> None:
        while not done.is_set():
            drained.extend(sink.drain())

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    writer_thread.start()
    writer_thread.join()
    reader_thread.join()
    drained.extend(sink.drain())

    assert len(drained) == total
