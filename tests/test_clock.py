from mvsync.clock import extrapolate


def test_paused_returns_position_unchanged():
    assert extrapolate(10.0, 100.0, 200.0, playing=False) == 10.0


def test_playing_advances_by_elapsed_time():
    assert extrapolate(10.0, 100.0, 102.5, playing=True) == 12.5


def test_clock_skew_never_rewinds():
    assert extrapolate(10.0, 100.0, 99.0, playing=True) == 10.0


def test_advance_is_capped():
    assert extrapolate(0.0, 0.0, 1000.0, playing=True, max_extrapolation_s=30.0) == 30.0
