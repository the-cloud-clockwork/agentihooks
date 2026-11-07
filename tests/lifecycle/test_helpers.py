import os

from hooks.lifecycle import helpers


def test_a_started_helper_is_live_for_its_session_only(tmp_path):
    helpers.start("main", "reader", tmp_path)
    assert helpers.live("main", tmp_path)
    assert not helpers.live("other", tmp_path)
    assert not helpers.live("", tmp_path)


def test_a_stopped_helper_is_gone_while_another_keeps_the_session_live(tmp_path):
    helpers.start("main", "spec", tmp_path)
    helpers.start("main", "standards", tmp_path)
    helpers.stop("main", "spec", tmp_path)
    assert helpers.live("main", tmp_path)
    helpers.stop("main", "standards", tmp_path)
    assert not helpers.live("main", tmp_path)


def test_a_record_past_its_time_to_live_counts_as_gone(tmp_path):
    helpers.start("main", "reader", tmp_path)
    record = helpers.folder("main", tmp_path) / "reader"
    os.utime(record, (1000, 1000))
    assert helpers.live("main", tmp_path, now=1000 + helpers.TTL_S - 1)
    assert not helpers.live("main", tmp_path, now=1000 + helpers.TTL_S)


def test_a_session_is_never_its_own_helper_and_blank_ids_record_nothing(tmp_path):
    helpers.start("main", "main", tmp_path)
    helpers.start("main", "", tmp_path)
    helpers.start("", "reader", tmp_path)
    helpers.stop("", "reader", tmp_path)
    assert not helpers.live("main", tmp_path)
    assert not (tmp_path / "helpers").exists()


def test_a_blank_stop_leaves_records_where_blank_ids_would_land(tmp_path):
    stray = tmp_path / "helpers" / "reader"
    stray.parent.mkdir()
    stray.touch()
    helpers.stop("", "reader", tmp_path)
    assert stray.exists()


def test_stopping_a_helper_never_recorded_is_quiet(tmp_path):
    helpers.stop("main", "never", tmp_path)
    assert not helpers.live("main", tmp_path)


def test_unsafe_characters_become_one_underscore_each():
    assert helpers.safe("a/b c") == "a_b_c"


def test_ids_cannot_escape_the_helpers_folder(tmp_path):
    root = tmp_path / "root"
    home = root / "home"
    helpers.start("../up", "../../out", home)
    assert helpers.live("../up", home)
    assert sorted(path.name for path in root.iterdir()) == ["home"]
    assert sorted(path.name for path in home.iterdir()) == ["helpers"]


def test_dot_ids_stay_inside_the_helpers_folder(tmp_path):
    root = tmp_path / "root"
    home = root / "home"
    helpers.start("..", "reader", home)
    helpers.start(".", "..", home)
    assert helpers.live("..", home)
    assert sorted(path.name for path in root.iterdir()) == ["home"]
    assert sorted(path.name for path in home.iterdir()) == ["helpers"]
    assert [path.name for path in (home / "helpers").iterdir()] == ["_"]
    assert sorted(path.name for path in (home / "helpers" / "_").iterdir()) == ["_", "reader"]


def test_a_payload_records_and_clears_its_helper_under_the_parent_session(tmp_path):
    payload = {"session_id": "main", "agent_id": "reader"}
    helpers.begin(payload, tmp_path)
    assert [path.name for path in helpers.folder("main", tmp_path).iterdir()] == ["reader"]
    helpers.end(payload, tmp_path)
    assert not helpers.live("main", tmp_path)


def test_a_payload_names_its_session_and_helper_or_blanks():
    assert helpers.ids({"session_id": "main", "agent_id": "reader"}) == ("main", "reader")
    assert helpers.ids({"session_id": None}) == ("", "")


def test_a_filesystem_failure_records_and_clears_nothing(tmp_path):
    home = tmp_path / "file"
    home.write_text("")
    payload = {"session_id": "main", "agent_id": "reader"}
    helpers.begin(payload, home)
    helpers.end(payload, home)
    assert home.read_text() == ""


def test_the_default_folder_sits_under_the_agentihooks_home(monkeypatch, tmp_path):
    from hooks import config

    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", str(tmp_path))
    assert helpers.folder("main") == tmp_path / "helpers" / "main"


def test_a_record_cleared_while_it_is_read_counts_as_gone(tmp_path):
    assert not helpers.fresh(tmp_path / "gone", 0)
