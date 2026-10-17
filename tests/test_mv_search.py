import pytest

from mvsync.cli import _parse_menu_args
from mvsync.mv_search import (
    Candidate,
    candidate_lines,
    parse_search_output,
    pick_best,
    prioritize,
    rank_candidates,
)


def test_menu_args_parse_pick_and_url():
    assert _parse_menu_args("3") == ("", None)
    assert _parse_menu_args("2 --pick 3") == ("", 3)
    assert _parse_menu_args("3 https://youtu.be/x --pick 2") == ("https://youtu.be/x", 2)
    assert _parse_menu_args("2 https://youtu.be/x") == ("https://youtu.be/x", None)
    assert _parse_menu_args("3 --pick") == ("--pick", None)


def test_parse_search_output_skips_junk_and_missing_ids():
    text = (
        '{"id": "abc", "title": "T", "duration": 100, "channel": "C"}\n'
        "not json\n"
        '{"title": "no id"}\n'
        '{"id": "def", "title": "U", "uploader": "UP"}\n'
    )
    candidates = parse_search_output(text)
    assert [(candidate.video_id, candidate.title) for candidate in candidates] == [
        ("abc", "T"),
        ("def", "U"),
    ]
    assert candidates[0].duration_s == 100.0
    assert candidates[0].channel == "C"
    assert candidates[1].duration_s is None
    assert candidates[1].channel == "UP"


def test_rank_prefers_official_mv_over_variants():
    candidates = [
        Candidate("live", "TAEYEON To. X Live Clip", 180.0, "TAEYEON Official"),
        Candidate("mv", "TAEYEON 'To. X' MV", 178.0, "SMTOWN"),
        Candidate("audio", "TAEYEON To. X (Audio)", 170.4, "x"),
        Candidate("remix", "To. X IMLAY Remix MV", 198.0, "SMTOWN"),
    ]
    ranked = rank_candidates(candidates, 170.4, "TAEYEON", "To. X")
    assert ranked[0].video_id == "mv"


def test_rank_prefers_official_mv_over_fansub_upload():
    candidates = [
        Candidate(
            "bDtswJoIb_I",
            "Taeyeon - 11:11 MV [English subs + Romanization + Hangul] HD",
            223.0,
            "LoveKpopSubs",
        ),
        Candidate("ulr0muQKjk0", "TAEYEON 태연 '11:11' MV", 327.0, "SMTOWN"),
    ]
    ranked = rank_candidates(candidates, 223.5, "TAEYEON", "11:11")
    assert ranked[0].video_id == "ulr0muQKjk0"


def test_pick_best_filters_impossible_durations():
    candidates = [
        Candidate("toolong", "T MV", 900.0, "C"),
        Candidate("ok", "T MV", 180.0, "C"),
    ]
    assert pick_best(candidates, 170.4, "a", "t").video_id == "ok"
    assert pick_best([candidates[0]], 170.4, "a", "t") is None


def test_pick_best_allows_unknown_durations():
    candidates = [Candidate("unknown", "T MV", None, "C")]
    assert pick_best(candidates, 170.4, "a", "t").video_id == "unknown"


def test_prioritize_moves_choice_to_front_as_fallback_order():
    candidates = [Candidate(str(index), "", None, "") for index in range(4)]
    reordered = prioritize(candidates, 3)
    assert [candidate.video_id for candidate in reordered] == ["2", "0", "1", "3"]


def test_prioritize_none_keeps_order():
    candidates = [Candidate("a", "", None, ""), Candidate("b", "", None, "")]
    assert prioritize(candidates, None) == candidates


def test_prioritize_out_of_range_raises():
    candidates = [Candidate("a", "", None, "")]
    with pytest.raises(ValueError):
        prioritize(candidates, 4)
    with pytest.raises(ValueError):
        prioritize(candidates, 0)


def test_candidate_lines_number_and_format():
    lines = candidate_lines(
        [
            Candidate("v1", "Song MV", 200.0, "Channel"),
            Candidate("v2", "", None, ""),
        ]
    )
    assert lines == [
        "candidate 1: Channel: Song MV (v1)",
        "candidate 2: (given url) (v2)",
    ]


def test_rank_prefers_track_artist_remake_mv_over_original_artist_mv():
    candidates = [
        Candidate(
            "VVO05mYGFY8",
            "[MV] 태연 (TAEYEON)_ 만찬가 (晩餐歌 / BANSANKA) : J-POP REMAKE Vol.1",
            222.0,
            "1theK (원더케이)",
        ),
        Candidate(
            "H0GbvPY_Yl8",
            "[MV] TAEYEON_ BANSANKA : J-POP REMAKE Vol.1",
            222.0,
            "동네친구 강나미 [Kangnami]",
        ),
        Candidate("oZpYEEcvu5I", "tuki.『Bansanka』Official Music Video", 220.0, "tuki.(17)"),
    ]
    ranked = rank_candidates(candidates, 219.0, "TAEYEON", "Bansanka")
    assert ranked[0].video_id == "VVO05mYGFY8"


def test_score_gives_the_official_mv_phrase_bonus_once():
    from mvsync.mv_search import _score

    plain = Candidate("a", "TAEYEON To. X MV", 178.0, "SMTOWN")
    phrase = Candidate("b", "TAEYEON To. X Official Music Video", 178.0, "SMTOWN")
    assert (
        _score(phrase, 170.4, "TAEYEON", "To. X")
        == _score(plain, 170.4, "TAEYEON", "To. X") + 1.0
    )
