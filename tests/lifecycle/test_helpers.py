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


def test_ids_cannot_escape_the_helpers_folder(tmp_path):
    home = tmp_path / "home"
    helpers.start("../up", "../../out", home)
    assert helpers.live("../up", home)
    assert sorted(path.name for path in tmp_path.iterdir() if path.name != "_home") == ["home"]
    assert sorted(path.name for path in home.iterdir()) == ["helpers"]


def test_dot_ids_stay_inside_the_helpers_folder(tmp_path):
    home = tmp_path / "home"
    helpers.start("..", "reader", home)
    helpers.start(".", "..", home)
    assert helpers.live("..", home)
    assert sorted(path.name for path in home.iterdir()) == ["helpers"]
    assert [path.name for path in (home / "helpers").iterdir()] == ["_"]
    assert sorted(path.name for path in (home / "helpers" / "_").iterdir()) == ["_", "reader"]


def test_a_payload_records_and_clears_its_helper_under_the_parent_session(tmp_path):
    payload = {"session_id": "main", "agent_id": "reader"}
    helpers.track(payload, started=True, home=tmp_path)
    assert helpers.live("main", tmp_path)
    helpers.track(payload, started=False, home=tmp_path)
    assert not helpers.live("main", tmp_path)


def test_the_default_folder_sits_under_the_agentihooks_home(monkeypatch, tmp_path):
    from hooks import config

    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", str(tmp_path))
    assert helpers.folder("main") == tmp_path / "helpers" / "main"


def test_a_record_cleared_while_it_is_read_counts_as_gone(tmp_path):
    assert not helpers.fresh(tmp_path / "gone", 0)
