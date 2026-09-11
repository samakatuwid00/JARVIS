"""app_abilities: sorting apps, predefined abilities per kind and profile,
keeping the user's choices, generate / full scan / add an app, reading
abilities from a window, and the runner's safety checks. The AI, windows,
and app launches are always faked; registries live in tmp_path."""

import json

import pytest

import app_abilities as aa


@pytest.fixture
def registry(tmp_path, monkeypatch):
    import machine_capabilities as mc
    path = tmp_path / "app_registry.json"
    monkeypatch.setattr(mc, "REGISTRY_PATH", str(path))

    def write(apps):
        path.write_text(json.dumps({"apps": apps}), encoding="utf-8")

    def read():
        return json.loads(path.read_text(encoding="utf-8"))["apps"]
    return write, read


def _llm(mapping, seen=None):
    def llm(prompt):
        if seen is not None:
            seen.append(prompt)
        return dict(mapping), "fake"
    return llm


# ------------------------------------------------------------------ sorting --

def test_heuristic_kind_without_ai():
    assert aa.heuristic_kind("spotify", {}) == "media"
    assert aa.heuristic_kind("asusscreenxperttoast", {}) == "noise"
    assert aa.heuristic_kind("fooupdater", {"bin": "C:/x/fooupdater.exe"}) == "noise"
    assert aa.heuristic_kind("photoshop", {"category": "edit"}) == "editor"
    assert aa.heuristic_kind("mystery", {"category": "other"}) == "utility"


def test_sort_apps_batches_and_falls_back():
    apps = {f"app{i}": {"bin": f"C:/a/app{i}.exe"} for i in range(45)}
    apps["spotify"] = {}
    seen = []
    kinds = aa.sort_apps(apps, llm=_llm({"app0": "game", "app1": "nonsense"}, seen))
    assert len(seen) == 2                      # 45 apps in batches of 40
    assert kinds["spotify"] == "media"         # profile, never sent to the AI
    assert kinds["app0"] == "game"
    assert kinds["app1"] == "utility"          # invalid kind -> heuristic


# ---------------------------------------------------------------- abilities --

def test_every_app_gets_open_close_switch_and_its_kind_template():
    ids = [a["id"] for a in aa.build_abilities("spotify", {"name": "spotify"}, "media")]
    assert ids[:3] == ["spotify.open", "spotify.close", "spotify.switch"]
    assert "spotify.play_pause" in ids and "spotify.play_liked" in ids
    word = {a["id"]: a for a in aa.build_abilities("winword", {"name": "winword"}, "office")}
    assert word["winword.close"]["level"] == "ask"      # unsaved work
    assert word["winword.save"]["level"] == "ask"
    term = {a["id"]: a for a in aa.build_abilities("cmd", {}, "terminal")}
    assert term["cmd.run_command"]["level"] == "never"


def test_rebuild_keeps_the_users_choices_and_scanned_abilities():
    old = [dict(aa.build_abilities("vlc", {}, "media")[3], enabled=False, level="ask"),
           {"id": "vlc.click_playlist", "source": "scan", "name": "Click Playlist"}]
    merged = {a["id"]: a for a in aa.merge_abilities(aa.build_abilities("vlc", {}, "media"), old)}
    assert merged["vlc.play_pause"]["enabled"] is False
    assert merged["vlc.play_pause"]["level"] == "ask"
    assert "vlc.click_playlist" in merged


# ----------------------------------------------------- generate / scan / add --

def test_generate_creates_abilities_and_hides_noise(registry):
    write, read = registry
    write({"spotify": {"name": "spotify", "registered": True},
           "glidexservice": {"name": "glidexservice", "registered": True},
           "keepme": {"name": "keepme", "registered": True, "hidden": False}})
    out = aa.generate(llm=_llm({"keepme": "noise"}))
    apps = read()
    assert out["apps"] == 3
    assert apps["spotify"]["app_kind"] == "media" and apps["spotify"]["abilities"]
    assert apps["glidexservice"]["hidden"] is True and "abilities" not in apps["glidexservice"]
    assert apps["keepme"]["hidden"] is False        # the user un-hid it: stays visible


def test_full_scan_merges_sorts_hides_and_registers(registry, monkeypatch):
    import machine_capabilities as mc
    write, read = registry
    write({"brave": {"name": "brave", "registered": False, "app_kind": "browser"}})

    def fake_scan():
        apps = read()
        apps.update({"obs64": {"name": "obs64"}, "asusupdate": {"name": "asusupdate"}})
        write(apps)
    monkeypatch.setattr(mc, "write_registry", fake_scan)
    out = aa.full_scan(llm=_llm({"obs64": "media"}))
    apps = read()
    assert apps["obs64"]["registered"] is True and apps["obs64"]["app_kind"] == "media"
    assert apps["asusupdate"]["hidden"] is True
    assert apps["brave"]["registered"] is False     # the user's own choice
    assert out == {"found": 3, "sorted": 2, "hidden": 1, "newly_registered": 1}


def test_add_app_by_program_path(registry, tmp_path):
    write, read = registry
    write({})
    exe = tmp_path / "Figma.exe"
    exe.write_text("")
    out = aa.add_app(str(exe), llm=_llm({"figma": "editor"}), scan=False)
    apps = read()
    assert out["key"] == "figma" and out["kind"] == "editor"
    assert apps["figma"]["added_by_user"] is True and apps["figma"]["registered"] is True
    assert any(a["id"] == "figma.open_folder" for a in apps["figma"]["abilities"])


def test_add_app_explains_an_unknown_name(registry, monkeypatch):
    import machine_capabilities as mc
    monkeypatch.setattr(mc, "resolve_candidates", lambda q: (None, []))
    assert "couldn't find" in aa.add_app("nothing-here", scan=False)["error"]


# ---------------------------------------------------------------- deep scan --

class _Info:
    def __init__(self, name, control_type):
        self.name, self.control_type = name, control_type


class _Ctrl:
    def __init__(self, name, control_type):
        self.element_info = _Info(name, control_type)


class _Window:
    def __init__(self, controls):
        self._c = [_Ctrl(n, t) for n, t in controls]

    def descendants(self):
        return self._c


def test_abilities_from_a_window():
    win = _Window([("Minimize", "Button"), ("Liked Songs", "Button"), ("Liked Songs", "Button"),
                   ("Delete playlist", "MenuItem"), ("Search", "Edit"), ("7", "Button"),
                   ("", "Button")])
    got = {a["name"]: a for a in aa.abilities_from_window("spotify", "Spotify", win)}
    assert set(got) == {"Click \u201cLiked Songs\u201d", "Click \u201cDelete playlist\u201d",
                        "Type into \u201cSearch\u201d"}
    assert got["Click \u201cDelete playlist\u201d"]["level"] == "ask"
    assert got["Type into \u201cSearch\u201d"]["needs"] == {"text": "text"}


def test_scan_caps_the_number_of_abilities():
    win = _Window([(f"Button {i:02d} x", "Button") for i in range(100)])
    assert len(aa.abilities_from_window("x", "X", win)) == aa.MAX_SCANNED


# ------------------------------------------------------------------ running --

def _app_with(registry, ability):
    write, _ = registry
    write({"spotify": {"name": "spotify", "abilities": [ability]}})


def test_runner_respects_never_off_and_missing_values(registry):
    base = {"id": "spotify.search", "name": "Search Spotify", "needs": {"query": "text"},
            "how": [{"op": "uri", "uri": "spotify:search:{query}"}], "level": "safe"}
    _app_with(registry, dict(base, level="never"))
    assert "set to never" in aa.run_ability("spotify", "spotify.search", {"query": "x"})
    _app_with(registry, dict(base, enabled=False))
    assert "switched off" in aa.run_ability("spotify", "spotify.search", {"query": "x"})
    _app_with(registry, base)
    assert "needs a query" in aa.run_ability("spotify", "spotify.search", {})


def test_runner_refuses_unsafe_links_paths_and_addresses(registry, monkeypatch):
    import os
    import tools
    started = []
    monkeypatch.setattr(os, "startfile", lambda p: started.append(p), raising=False)
    monkeypatch.setattr(tools, "_open_url_in_browser",
                        lambda url, *a, **k: started.append(url) or "Opened.")
    for how, args in [([{"op": "uri", "uri": "{link}"}], {"link": "file:///C:/x.bat"}),
                      ([{"op": "open_path", "path": "{path}"}], {"path": "C:/no/such/place"}),
                      ([{"op": "open_url", "url": "{site}"}], {"site": "--gpu-launcher=calc"})]:
        _app_with(registry, {"id": "spotify.x", "name": "X", "needs": {k: "text" for k in args},
                             "how": how, "level": "safe"})
        assert aa.run_ability("spotify", "spotify.x", args).startswith("[Error]")
    assert started == []


def test_open_folder_never_runs_a_program(registry, monkeypatch, tmp_path):
    import os
    started = []
    monkeypatch.setattr(os, "startfile", lambda p: started.append(p), raising=False)
    monkeypatch.setattr("audit.log_call", lambda *a, **k: None)
    program = tmp_path / "evil.bat"
    program.write_text("echo hi")
    _app_with(registry, {"id": "spotify.f", "name": "Open a folder", "needs": {"path": "folder"},
                         "level": "safe", "how": [{"op": "open_path", "path": "{path}"}]})
    assert aa.run_ability("spotify", "spotify.f", {"path": str(program)}).endswith("isn't a folder.")
    assert aa.run_ability("spotify", "spotify.f", {"path": str(tmp_path)}) == "Done: Open a folder."
    assert started == [str(tmp_path)]


def test_runner_encodes_names_into_allowed_links(registry, monkeypatch):
    import os
    started = []
    monkeypatch.setattr(os, "startfile", lambda p: started.append(p), raising=False)
    monkeypatch.setattr("audit.log_call", lambda *a, **k: None)
    _app_with(registry, {"id": "spotify.search", "name": "Search Spotify",
                         "needs": {"query": "text"}, "level": "safe",
                         "how": [{"op": "uri", "uri": "spotify:search:{query}"}]})
    assert aa.run_ability("spotify", "spotify.search", {"query": "Daft Punk"}) == \
        "Done: Search Spotify."
    assert started == ["spotify:search:Daft%20Punk"]


def test_runner_refuses_unsafe_keys(registry, monkeypatch):
    class Win:
        typed = []

        def set_focus(self):
            pass

        def type_keys(self, keys):
            Win.typed.append(keys)
    monkeypatch.setattr(aa, "find_app_window", lambda entry, timeout=1.0: Win())
    _app_with(registry, {"id": "spotify.k", "name": "K", "level": "safe",
                         "how": [{"op": "key", "keys": "%{F4}"}]})
    assert aa.run_ability("spotify", "spotify.k").startswith("[Error] I won't press")
    assert Win.typed == []


class _ClosableWindow(_Window):
    closed = 0

    def close(self):
        _ClosableWindow.closed += 1


def test_scan_closes_only_the_window_it_opened(registry, monkeypatch):
    import tools
    write, read = registry
    write({"figma": {"name": "figma", "bin": "C:/Figma.exe", "abilities": []}})
    windows = iter([None, _ClosableWindow([("Share", "Button")])])
    monkeypatch.setattr(aa, "find_app_window", lambda entry, timeout=1.0: next(windows))
    monkeypatch.setattr(tools, "open_application", lambda key: "Opened Figma.")
    monkeypatch.setattr(tools, "close_application",
                        lambda key: (_ for _ in ()).throw(AssertionError("ended the program")))
    _ClosableWindow.closed = 0
    out = aa.scan_app("figma", launch=True)
    assert out == {"found": 1, "opened": True} and _ClosableWindow.closed == 1
    assert read()["figma"]["deep_scan"]["found"] == 1


def test_closing_file_explorer_never_ends_explorer_exe(registry, monkeypatch):
    import tools
    write, _ = registry
    write({"explorer": {"name": "explorer", "abilities": aa.build_abilities("explorer", {}, "files")}})
    monkeypatch.setattr(aa, "find_app_window", lambda entry, timeout=1.0: _ClosableWindow([]))
    monkeypatch.setattr(tools, "close_application",
                        lambda key: (_ for _ in ()).throw(AssertionError("killed the shell")))
    monkeypatch.setattr("audit.log_call", lambda *a, **k: None)
    _ClosableWindow.closed = 0
    assert aa.run_ability("explorer", "explorer.close") == "Done: Close File Explorer."
    assert _ClosableWindow.closed == 1


def test_known_apps_use_their_real_names():
    names = [a["name"] for a in aa.build_abilities("winword", {"name": "winword"}, "office")]
    assert names[0] == "Open Word"


def test_set_choice(registry):
    write, read = registry
    write({"vlc": {"abilities": aa.build_abilities("vlc", {}, "media")}})
    aa.set_choice("vlc", "vlc.next", enabled=False, level="ask")
    aa.set_choice("vlc", "vlc.next", level="sometimes")        # not a level: ignored
    a = next(x for x in read()["vlc"]["abilities"] if x["id"] == "vlc.next")
    assert a["enabled"] is False and a["level"] == "ask"
