def test_guess_player_exe_matches_the_process_stem(monkeypatch):
    import psutil

    from mvsync.player_process import guess_player_exe

    class Process:
        def __init__(self, name):
            self.info = {"name": name}

    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [Process("Spotify.exe")])
    assert guess_player_exe("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify") == "spotify.exe"


def test_guess_player_exe_returns_none_for_an_unknown_app(monkeypatch):
    import psutil

    from mvsync.player_process import guess_player_exe

    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [])
    assert guess_player_exe("some.unknown.app") is None


def test_guess_player_exe_ignores_an_empty_app_id():
    from mvsync.player_process import guess_player_exe

    assert guess_player_exe("") is None
