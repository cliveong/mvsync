import json

from mvsync import config, paths


def write_config(root, payload):
    path = paths.config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_config(root):
    return json.loads(paths.config_path(root).read_text(encoding="utf-8"))


def test_capture_mode_defaults_to_process(tmp_path):
    assert config.load_capture_mode(tmp_path) == "process"


def test_legacy_spotify_mode_maps_to_process(tmp_path):
    write_config(tmp_path, {"capture_mode": "spotify"})
    assert config.load_capture_mode(tmp_path) == "process"


def test_set_capture_mode_preserves_other_keys(tmp_path):
    write_config(tmp_path, {"dev_log": True, "player": "vlc.exe"})
    config.set_capture_mode("device", tmp_path)
    assert read_config(tmp_path) == {
        "capture_mode": "device",
        "dev_log": True,
        "player": "vlc.exe",
    }


def test_save_player_preserves_other_keys(tmp_path):
    config.set_capture_mode("device", tmp_path)
    config.save_player("vlc.exe", "vlc.exe", tmp_path)
    assert config.load_player(tmp_path) == "vlc.exe"
    assert config.load_player_exe(tmp_path) == "vlc.exe"
    assert config.load_capture_mode(tmp_path) == "device"


def test_save_player_without_an_exe_clears_the_old_one(tmp_path):
    config.save_player("vlc.exe", "vlc.exe", tmp_path)
    config.save_player("musicbee.exe", "", tmp_path)
    assert config.load_player_exe(tmp_path) == ""


def test_max_video_height_defaults_to_1080(tmp_path):
    assert config.load_max_video_height(tmp_path) == 1080


def test_max_video_height_reads_a_valid_override(tmp_path):
    write_config(tmp_path, {"max_video_height": 1080})
    assert config.load_max_video_height(tmp_path) == 1080


def test_max_video_height_ignores_invalid_values(tmp_path):
    for value in (None, "1080", 0, 143, 4321, True):
        write_config(tmp_path, {"max_video_height": value})
        assert config.load_max_video_height(tmp_path) == 1080
