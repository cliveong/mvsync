import json

from mvsync import paths, video_choice


def test_video_choice_round_trip(tmp_path):
    video_choice.save_video_choice("track-a", "video-a", tmp_path)

    assert video_choice.load_video_choice("track-a", tmp_path) == "video-a"
    assert video_choice.load_video_choice("track-b", tmp_path) is None


def test_video_choice_keeps_other_tracks_when_saving(tmp_path):
    video_choice.save_video_choice("track-a", "video-a", tmp_path)
    video_choice.save_video_choice("track-b", "video-b", tmp_path)

    assert video_choice.load_video_choice("track-a", tmp_path) == "video-a"
    assert video_choice.load_video_choice("track-b", tmp_path) == "video-b"


def test_video_choice_ignores_corrupt_json(tmp_path):
    path = paths.video_choices_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    assert video_choice.load_video_choice("track-a", tmp_path) is None
    video_choice.save_video_choice("track-a", "video-a", tmp_path)
    assert video_choice.load_video_choice("track-a", tmp_path) == "video-a"


def test_video_choice_ignores_bad_entries(tmp_path):
    path = paths.video_choices_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"track-a": {"video_id": 4}}), encoding="utf-8")

    assert video_choice.load_video_choice("track-a", tmp_path) is None


def test_clear_video_choice_removes_only_that_track(tmp_path):
    video_choice.save_video_choice("track-a", "video-a", tmp_path)
    video_choice.save_video_choice("track-b", "video-b", tmp_path)

    video_choice.clear_video_choice("track-a", tmp_path)

    assert video_choice.load_video_choice("track-a", tmp_path) is None
    assert video_choice.load_video_choice("track-b", tmp_path) == "video-b"


def test_clear_video_choice_if_video_keeps_a_different_choice(tmp_path):
    video_choice.save_video_choice("track-a", "video-b", tmp_path)

    video_choice.clear_video_choice_if_video("track-a", "video-a", tmp_path)
    assert video_choice.load_video_choice("track-a", tmp_path) == "video-b"

    video_choice.clear_video_choice_if_video("track-a", "video-b", tmp_path)
    assert video_choice.load_video_choice("track-a", tmp_path) is None


def test_video_choice_rejects_blank_values(tmp_path):
    for track_id, video_id in (("", "video-a"), ("track-a", "")):
        try:
            video_choice.save_video_choice(track_id, video_id, tmp_path)
        except ValueError:
            pass
        else:
            raise AssertionError("blank choice should be rejected")


def test_choice_mutations_hold_the_shared_lock(tmp_path, monkeypatch):
    locked = []
    original = video_choice._load

    def probe(root):
        locked.append(video_choice._LOCK.locked())
        return original(root)

    monkeypatch.setattr(video_choice, "_load", probe)

    video_choice.save_video_choice("track-a", "video-a", tmp_path)

    assert locked == [True]
