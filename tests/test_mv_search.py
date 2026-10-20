import pytest

from mvsync import mv_search
from mvsync.cli import _parse_menu_args
from mvsync.mv_search import (
    Candidate,
    candidate_lines,
    parse_search_output,
    pick_best,
    pick_mv_candidates,
    prioritize,
    rank_candidates,
)


def test_menu_args_parse_pick_and_url():
    assert _parse_menu_args("3") == ("", None)
    assert _parse_menu_args("2 --pick 3") == ("", 3)
    assert _parse_menu_args("3 https://youtu.be/x --pick 2") == ("https://youtu.be/x", 2)
    assert _parse_menu_args("2 https://youtu.be/x") == ("https://youtu.be/x", None)
    assert _parse_menu_args("3 --pick") == ("--pick", None)


def test_parse_search_output_keeps_description():
    text = '{"id": "abc", "title": "T", "description": "Provided to YouTube by X"}\n'
    assert parse_search_output(text)[0].description == "Provided to YouTube by X"


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


def test_rank_prefers_non_variant_mv_over_closer_remix_mv():
    candidates = [
        Candidate("official", "TAEYEON To. X MV", 200.0, "SMTOWN"),
        Candidate("remix", "TAEYEON To. X IMLAY Remix MV", 171.0, "SMTOWN"),
    ]
    ranked = rank_candidates(candidates, 170.4, "TAEYEON", "To. X")
    assert ranked[0].video_id == "official"


def test_content_priority_classifies_variants_before_generic_mv():
    from mvsync.mv_search import _content_priority

    official = Candidate("official", "TAEYEON To. X MV", 200.0, "SMTOWN")
    remix = Candidate("remix", "TAEYEON To. X IMLAY Remix MV", 171.0, "SMTOWN")
    assert _content_priority(official, "TAEYEON") > _content_priority(remix, "TAEYEON")


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


def test_rank_orders_mv_before_official_audio_before_official_live():
    candidates = [
        Candidate("live", "NMIXX 너는 달 (Sing to the Moon) (Official Live)", 240.0, "NMIXX"),
        Candidate("audio", "NMIXX 너는 달 (Sing to the Moon) (Official Audio)", 240.0, "NMIXX"),
        Candidate("mv", "NMIXX 너는 달 (Sing to the Moon) MV", 240.0, "NMIXX"),
    ]
    ranked = rank_candidates(candidates, 240.0, "NMIXX", "Sing to the Moon")
    assert [candidate.video_id for candidate in ranked] == ["mv", "audio", "live"]


def test_official_channel_live_loses_only_one_point():
    from mvsync.mv_search import _score

    live = Candidate("live", "NMIXX Sing to the Moon (Live)", 240.0, "NMIXX Official")
    studio = Candidate("studio", "NMIXX Sing to the Moon", 240.0, "NMIXX Official")
    assert (
        _score(studio, 240.0, "NMIXX", "Sing to the Moon")
        == _score(live, 240.0, "NMIXX", "Sing to the Moon") + 1.0
    )


def test_rank_puts_title_matches_before_same_artist_mvs():
    candidates = [
        Candidate("random", "NMIXX Blue Valentine M/V", 195.0, "JYP Entertainment"),
        Candidate("right", "너는 달 (Sing to the Moon) (LILY, HAEWON)", 240.0, "NMIXX"),
    ]
    ranked = rank_candidates(candidates, 240.0, "NMIXX", "Sing to the Moon")
    assert [candidate.video_id for candidate in ranked] == ["right", "random"]


def test_rank_prefers_label_provided_upload_over_wrong_artist_mv():
    candidates = [
        Candidate(
            "wrong",
            "KIDZ BOP Kids - Do You Remember (Official Music Video) [KIDZ BOP 18]",
            179.0,
            "KIDZ BOP",
            "KIDZ BOP Kids sing Do You Remember",
        ),
        Candidate(
            "live",
            "도경수 Doh Kyung Soo '놀이터 (Do you remember?)' Live Clip (Preview)",
            133.0,
            "컴퍼니수수 Company Soosoo",
            "도경수 #Dohkyungsoo #BLISS",
        ),
        Candidate(
            "target",
            "Do you remember?",
            178.0,
            "도경수(DOH KYUNG SOO)",
            "Provided to YouTube by FLUXUS Do you remember? · D.O. The 1st full album [BLISS]",
        ),
        Candidate(
            "lyric",
            "Doh Kyung Soo (D.O.) 'Do you remember?' Lyrics (도경수 놀이터 가사)",
            180.0,
            "ShadowByYoongi",
            "Artist: Doh Kyung Soo Song: Do you remember?",
        ),
        Candidate(
            "fancam",
            "250720 도경수 (D.O.) 놀이터 (Do you remember?) DO it!",
            189.0,
            "롹",
            "",
        ),
    ]
    ranked = rank_candidates(candidates, 177.133, "D.O.", "Do you remember?")
    assert ranked[0].video_id == "target"


def test_pick_mv_candidates_finds_official_audio_for_b_side(monkeypatch):
    mv_results = [
        Candidate("random", "NMIXX Blue Valentine M/V", 195.0, "JYP Entertainment"),
        Candidate("same", "너는 달 (Sing to the Moon) (LILY, HAEWON)", 240.0, "NMIXX"),
    ]

    def fake_search(query, *, root=None):
        if query.endswith(" MV"):
            return mv_results
        return [
            Candidate(
                "H6Uv2xfdo0I",
                "NMIXX 너는 달 (Sing to the Moon) (Official Audio)",
                240.0,
                "NMIXX",
            ),
            *mv_results,
        ]

    monkeypatch.setattr(mv_search, "search_candidates", fake_search)
    ranked = pick_mv_candidates("NMIXX", "Sing to the Moon", 240.0)
    assert ranked[0].video_id == "H6Uv2xfdo0I"


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


def test_rank_prefers_exact_short_title_over_longer_title_containing_it():
    candidates = [
        Candidate("wrong", "TAEYEON I Got Love MV", 220.0, "SMTOWN"),
        Candidate("right", "TAEYEON 태연 'I (feat. Verbal Jint)' MV", 180.0, "SMTOWN"),
    ]
    ranked = rank_candidates(candidates, 200.0, "TAEYEON", "I")
    assert ranked[0].video_id == "right"


def test_rank_matches_short_korean_title_with_unquoted_artist_prefix():
    candidates = [
        Candidate("wrong", "TAEYEON I Got Love MV", 220.0, "SMTOWN"),
        Candidate("right", "태연 사계 MV", 218.0, "SMTOWN"),
    ]
    ranked = rank_candidates(candidates, 218.0, "TAEYEON", "사계")
    assert ranked[0].video_id == "right"


def test_score_penalizes_plural_lyrics_upload():
    from mvsync.mv_search import _score

    clean = Candidate("clean", "TAEYEON To. X", 170.4, "SMTOWN")
    lyrics = Candidate("lyrics", "TAEYEON To. X (Lyrics)", 170.4, "SMTOWN")
    assert (
        _score(clean, 170.4, "TAEYEON", "To. X")
        == _score(lyrics, 170.4, "TAEYEON", "To. X") + 2.0
    )


def test_score_title_bonus_needs_whole_words():
    from mvsync.mv_search import _score

    incidental = Candidate("other", "TAEYEON 태연 'INVU' MV", 218.0, "SMTOWN")
    true_match = Candidate("real", "TAEYEON 태연 'I (feat. Verbal Jint)' MV", 218.0, "SMTOWN")
    assert (
        _score(true_match, 206.0, "TAEYEON", "I")
        == _score(incidental, 206.0, "TAEYEON", "I") + 2.0
    )


def test_score_title_bonus_supports_korean_titles():
    from mvsync.mv_search import _score

    match = Candidate("kr", "TAEYEON 태연 '사계 (Four Seasons)' MV", 218.0, "SMTOWN")
    miss = Candidate("no", "TAEYEON 태연 'INVU' MV", 218.0, "SMTOWN")
    assert _score(match, 218.0, "TAEYEON", "사계") > _score(miss, 218.0, "TAEYEON", "사계")


def test_score_gives_the_official_mv_phrase_bonus_once():
    from mvsync.mv_search import _score

    plain = Candidate("a", "TAEYEON To. X MV", 178.0, "SMTOWN")
    phrase = Candidate("b", "TAEYEON To. X Official Music Video", 178.0, "SMTOWN")
    assert (
        _score(phrase, 170.4, "TAEYEON", "To. X")
        == _score(plain, 170.4, "TAEYEON", "To. X") + 1.0
    )
