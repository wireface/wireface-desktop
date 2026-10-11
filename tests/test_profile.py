import json

import pytest

from wireface import config, profile as profiles


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(config, "DEFAULT_CWD", tmp_path)
    return tmp_path / "data"


def test_the_default_face_is_the_original_data_folder(data):
    p = profiles.resolve()
    assert p.name == "default" and p.dir == data and p.legacy
    assert p.config["backend"]["type"] == "claude"
    assert p.config["backend"]["skip_permissions"] is False          # never on unless chosen
    assert p.face_file == data / "face.json" and p.window_file == data / "face_window.json"


def test_legacy_settings_json_is_read_as_the_first_profile(data, tmp_path):
    (data / "settings.json").write_text(json.dumps({
        "cwd": str(tmp_path), "permission_mode": "acceptEdits", "model": "opus", "voice": "alba",
        "voice_on": False, "listening": True, "session_id": "abc"}))
    p = profiles.resolve()
    assert p.config["backend"]["permission_mode"] == "acceptEdits"
    assert p.config["backend"]["model"] == "opus"
    assert p.config["voice"]["local"]["voice"] == "alba"
    assert p.config["voice"]["on"] is False and p.config["voice"]["listening"] is True
    assert p.state["session_id"] == "abc"


def test_profile_json_wins_over_legacy_settings(data):
    (data / "settings.json").write_text(json.dumps({"permission_mode": "plan"}))
    (data / "profile.json").write_text(json.dumps({"backend": {"permission_mode": "acceptEdits"}}))
    assert profiles.resolve().config["backend"]["permission_mode"] == "acceptEdits"


def test_invalid_saved_values_are_dropped_not_fatal(data):
    (data / "profile.json").write_text(json.dumps({
        "name": "Ada", "port": 99999, "backend": {"permission_mode": "bogus", "type": "nope"},
        "voice": {"engine": "elevenlabs"}}))
    p = profiles.resolve()
    assert p.config["name"] == "Ada" and p.config["voice"]["engine"] == "elevenlabs"
    assert p.config["port"] == 8791                                    # out of range: default kept
    assert p.config["backend"]["permission_mode"] == "default" and p.config["backend"]["type"] == "claude"


def test_a_missing_folder_forgets_its_conversation(data, tmp_path):
    (data / "profile.json").write_text(json.dumps({"backend": {"cwd": str(tmp_path / "gone")}}))
    (data / "state.json").write_text(json.dumps({"session_id": "old"}))
    p = profiles.resolve()
    assert p.config["backend"]["cwd"] == str(tmp_path) and p.state["session_id"] is None


def test_validate_checks_every_setting():
    base = profiles.defaults()
    ok = profiles.validate({"backend": {"skip_permissions": True}, "voice": {"local": {"voice": "x"}}}, base)
    assert ok["backend"]["skip_permissions"] is True and ok["voice"]["local"]["voice"] == "x"
    assert base["backend"]["skip_permissions"] is False                # the base is not changed
    for bad in ({"nope": 1}, {"backend": {"skip_permissions": "yes"}}, {"port": -1}, {"voice": {"engine": "x"}},
                {"backend": {"permission_mode": "bogus"}}, {"backend": {"cwd": "Z:\\no\\such\\dir"}},
                {"voice": {"elevenlabs": {"api_key_env": "not valid!"}}}, {"backend": {"options": [1]}}):
        with pytest.raises(ValueError):
            profiles.validate(bad, base)


def test_options_hold_anything_backend_specific():
    cfg = profiles.validate({"backend": {"options": {"effort": "high", "nested": {"a": 1}}}}, profiles.defaults())
    assert cfg["backend"]["options"] == {"effort": "high", "nested": {"a": 1}}


def test_faces_have_separate_folders_and_configs(data):
    a, b = profiles.resolve("alpha"), profiles.resolve("beta")
    assert a.dir == data / "faces" / "alpha" and b.dir == data / "faces" / "beta"
    assert a.config["port"] == 0                                       # new faces take any free port
    a.config = profiles.validate({"name": "Alpha"}, a.config)
    a.save_config()
    assert profiles.resolve("alpha").config["name"] == "Alpha"
    assert profiles.resolve("beta").config["name"] != "Alpha"


def test_config_option_points_at_any_file(data, tmp_path):
    f = tmp_path / "work.json"
    f.write_text(json.dumps({"name": "Work", "backend": {"skip_permissions": True}}))
    p = profiles.resolve(config_path=str(f))
    assert p.config["name"] == "Work" and p.config["backend"]["skip_permissions"] is True
    assert p.dir == data / "faces" / "work"
    p.save_config()
    assert json.loads(f.read_text())["name"] == "Work"                 # saved back to the same file


def test_a_config_saved_with_a_byte_order_mark_is_read(data, tmp_path):
    f = tmp_path / "bom.json"                                            # as PowerShell 5 and old Notepad save it
    f.write_text(chr(0xFEFF) + json.dumps({"name": "Bom", "port": 18799}), encoding="utf-8")
    p = profiles.resolve(config_path=str(f))
    assert p.load_error is None and p.config["name"] == "Bom" and p.config["port"] == 18799


def test_face_names_become_safe_folder_names():
    assert profiles.slug("My Face!") == "My-Face"
    with pytest.raises(ValueError):
        profiles.slug("///")


def test_one_instance_per_face(data, monkeypatch):
    p = profiles.resolve("solo")
    p.dir.mkdir(parents=True)
    p.lock_file.write_text("4242")
    monkeypatch.setattr(profiles, "_is_face_process", lambda pid: True)
    with pytest.raises(profiles.AlreadyRunning):
        p.acquire_lock()
    monkeypatch.setattr(profiles, "_is_face_process", lambda pid: False)      # a stale lock: pid reused or dead
    p.acquire_lock()
    assert p.lock_file.exists()
    p.release_lock()
    assert not p.lock_file.exists()


def test_a_broken_config_file_is_reported_and_kept(data):
    (data / "profile.json").write_text('{"name": "Ada"  oops')
    p = profiles.resolve()
    assert p.load_error and "not valid" in p.load_error and p.config["name"] == "Claude"
    p.save_config()                                                    # defaults replace it, but the original is kept
    assert "oops" in (data / "profile.json.bad").read_text()
    assert profiles.resolve().load_error is None


def test_a_config_of_the_wrong_shape_does_not_crash(data):
    (data / "profile.json").write_text(json.dumps({"backend": "nonsense", "voice": [1], "name": 5}))
    p = profiles.resolve()
    assert p.config["backend"]["type"] == "claude" and p.config["name"] == "Claude"
    (data / "profile.json").write_text(json.dumps({"backend": {"cwd": 12}}))
    assert profiles.resolve().state["session_id"] is None


def test_the_default_working_folder_is_a_sandbox_made_at_startup(data, monkeypatch):
    sandbox = data / "sandbox"
    monkeypatch.setattr(config, "SANDBOX", sandbox)
    monkeypatch.setattr(config, "DEFAULT_CWD", sandbox)
    assert not sandbox.exists()
    p = profiles.resolve()
    assert p.config["backend"]["cwd"] == str(sandbox)
    assert sandbox.is_dir() and (sandbox / "AGENTS.md").read_text(encoding="utf-8").startswith("# Instructions")
    (sandbox / "AGENTS.md").write_text("edited", encoding="utf-8")
    profiles.resolve()
    assert (sandbox / "AGENTS.md").read_text(encoding="utf-8") == "edited"      # never rewritten


def test_a_folder_chosen_in_the_environment_is_not_created(data, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SANDBOX", data / "sandbox")
    monkeypatch.setattr(config, "DEFAULT_CWD", tmp_path / "elsewhere")
    profiles.resolve()
    assert not (tmp_path / "elsewhere").exists() and not (data / "sandbox").exists()


def test_the_room_setting_is_validated():
    base = profiles.defaults()
    assert base["room"] == {"name": None, "aliases": [], "lead": "first"}
    ok = profiles.validate({"room": {"name": "team", "aliases": ["code x", " oc "], "lead": "Claude"}}, base)
    assert ok["room"] == {"name": "team", "aliases": ["code x", "oc"], "lead": "Claude"}
    assert profiles.validate({"room": {"name": None}}, ok)["room"]["name"] is None            # leave the room
    for bad in ({"room": {"name": "has space"}}, {"room": {"name": ""}}, {"room": {"aliases": "codex"}},
                {"room": {"aliases": [""]}}, {"room": {"aliases": ["a"] * 11}}, {"room": {"lead": ""}}, {"room": {"x": 1}}):
        with pytest.raises(ValueError):
            profiles.validate(bad, base)
