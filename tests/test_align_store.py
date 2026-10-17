import json
import os

import pytest

from mvsync.align_store import (
    SCHEMA_VERSION,
    alignment_from_dict,
    alignment_to_dict,
    find_alignments,
    load_alignment,
    make_track_id,
    save_alignment,
    video_id_from_alignment_path,
)
from mvsync.timeline import Alignment, Anchor, SkipSpan


def sample_alignment() -> Alignment:
    return Alignment(
        video_id="dQw4w9WgXcQ",
        track_id="rick-astley-never-gonna-give-you-up-1a2b3c4d5e",
        video_duration_s=213.0,
        track_duration_s=200.0,
        anchors=[Anchor(13.0, 0.0), Anchor(100.0, 87.0), Anchor(213.0, 200.0)],
        skips=[SkipSpan(100.0, 113.0, reason="internal", confidence=0.9)],
    )


def test_make_track_id_is_stable_and_file_safe():
    first = make_track_id("AC/DC", "T.N.T.")
    assert first == make_track_id("AC/DC", "T.N.T.")
    assert first == first.lower()
    assert all(character.isalnum() or character == "-" for character in first)
    assert first != make_track_id("ACDC", "T.N.T.")


def test_make_track_id_handles_empty_metadata():
    assert make_track_id("", "").startswith("track-")


def test_dict_roundtrip_preserves_everything():
    alignment = sample_alignment()
    data = alignment_to_dict(alignment, offset_s=1.5)
    assert data["version"] == SCHEMA_VERSION
    restored, offset = alignment_from_dict(data)
    assert offset == pytest.approx(1.5)
    assert restored.video_id == alignment.video_id
    assert restored.track_id == alignment.track_id
    assert restored.video_duration_s == alignment.video_duration_s
    assert restored.track_duration_s == alignment.track_duration_s
    assert restored.anchors == alignment.anchors
    assert restored.skips == alignment.skips


def test_save_and_load_roundtrip(tmp_path):
    alignment = sample_alignment()
    path = save_alignment(tmp_path, alignment, offset_s=0.25)
    assert path.name == f"{alignment.video_id}__{alignment.track_id}.json"
    loaded = load_alignment(tmp_path, alignment.video_id, alignment.track_id)
    assert loaded is not None
    restored, offset = loaded
    assert offset == pytest.approx(0.25)
    assert restored.anchors == alignment.anchors


def test_load_missing_returns_none(tmp_path):
    assert load_alignment(tmp_path, "nope", "nope") is None


def test_unsupported_version_rejected():
    with pytest.raises(ValueError):
        alignment_from_dict({"version": 999})


def test_save_is_atomic_and_overwrites(tmp_path):
    alignment = sample_alignment()
    save_alignment(tmp_path, alignment)
    save_alignment(tmp_path, alignment, offset_s=2.0)
    entries = list(tmp_path.iterdir())
    assert [entry.name for entry in entries] == [
        f"{alignment.video_id}__{alignment.track_id}.json"
    ]
    data = json.loads(entries[0].read_text(encoding="utf-8"))
    assert data["offset_s"] == 2.0


def test_find_alignments_returns_newest_first(tmp_path):
    track_id = "track-abc"
    older = tmp_path / f"aaa__{track_id}.json"
    newer = tmp_path / f"bbb__{track_id}.json"
    other = tmp_path / "ccc__track-other.json"
    for path in (older, newer, other):
        path.write_text("{}", encoding="utf-8")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    matches = find_alignments(tmp_path, track_id)
    assert [path.name for path in matches] == [newer.name, older.name]


def test_find_alignments_missing_dir(tmp_path):
    assert find_alignments(tmp_path / "nope", "track") == []


def test_video_id_from_alignment_path(tmp_path):
    path = tmp_path / "dQw4w9WgXcQ__track-abc.json"
    assert video_id_from_alignment_path(path) == "dQw4w9WgXcQ"
