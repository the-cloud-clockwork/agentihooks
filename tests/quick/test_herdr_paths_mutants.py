import json
from pathlib import Path

import pytest

from scripts import herdr_host, init_agent
from scripts.quick import paths
from scripts.quick.guard import QuickError
from scripts.quick.member import DIR_ENV, QUICK_ENV, SEAT_ENV

pytestmark = pytest.mark.unit


def test_existing_pane_asks_herdr_for_that_pane_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_cli(args: list[str], environ: dict) -> dict:
        calls.append((args, environ))
        return {"pane": {"workspace_id": "w1", "tab_id": "t1", "pane_id": "p1"}}

    monkeypatch.setattr(herdr_host, "_cli", fake_cli)
    placed = herdr_host.existing_pane("w1:p1", {"E": "1"})
    assert calls == [(["pane", "get", "w1:p1"], {"E": "1"})]
    assert placed.pane_id == "p1"


def test_process_info_asks_herdr_for_that_panes_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_cli(args: list[str], environ: dict) -> dict:
        calls.append((args, environ))
        return {"process_info": {"pid": 1}}

    monkeypatch.setattr(herdr_host, "_cli", fake_cli)
    result = herdr_host.process_info("w1:p1", {"E": "1"})
    assert calls == [(["pane", "process-info", "--pane", "w1:p1"], {"E": "1"})]
    assert result == {"pid": 1}


def test_columns_asks_herdr_for_the_panes_tab_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_cli(args: list[str], environ: dict) -> dict:
        calls.append((args, environ))
        return {"layout": {"panes": [{"pane_id": "p1", "rect": {"x": "5", "width": "10"}}]}}

    monkeypatch.setattr(herdr_host, "_cli", fake_cli)
    result = herdr_host.columns("w1:p1", {"E": "1"})
    assert calls == [(["pane", "layout", "--pane", "w1:p1"], {"E": "1"})]
    assert result == {"p1": (5, 10)}


def test_columns_defaults_to_no_panes_when_the_layout_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: {"layout": {}})
    assert herdr_host.columns("w1:p1", {}) == {}


def test_columns_defaults_to_no_panes_when_herdr_returns_no_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: {})
    assert herdr_host.columns("w1:p1", {}) == {}


def test_split_right_keeps_the_ratio_and_passes_this_sessions_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_cli(args: list[str], environ: dict) -> dict:
        calls.append((args, environ))
        return {"pane": {"workspace_id": "w1", "tab_id": "t1", "pane_id": "p2"}}

    monkeypatch.setattr(herdr_host, "_cli", fake_cli)
    herdr_host.split_right("w1:p1", Path("/work"), {"E": "1"}, 0.3)
    assert calls == [
        (
            ["pane", "split", "w1:p1", "--direction", "right", "--ratio", "0.3", "--cwd", "/work", "--no-focus"],
            {"E": "1"},
        )
    ]


def test_run_exports_env_assignments_before_the_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_cli(args: list[str], environ: dict) -> dict:
        calls.append((args, environ))
        return {}

    monkeypatch.setattr(herdr_host, "_cli", fake_cli)
    herdr_host.run("w1:p1", Path("/bin/launch.sh"), {"E": "1"}, {"A": "1", "B": "2"})
    assert calls == [(["pane", "run", "w1:p1", "A=1 B=2 exec /bin/launch.sh"], {"E": "1"})]


def test_home_prefers_agentihooks_home_over_home() -> None:
    environ = {"AGENTIHOOKS_HOME": "/x/y", "HOME": "/other"}
    assert paths.home(environ) == Path("/x/y")


def test_home_falls_back_to_this_users_home_dir() -> None:
    environ = {"HOME": "/u"}
    assert paths.home(environ) == Path("/u/.agentihooks")


def test_folder_for_uses_this_repos_label(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths.herdr_host, "repo_label", lambda directory: f"label:{directory}")
    result = paths.folder_for({"HOME": str(tmp_path)}, "quick-abc", Path("/some/repo"))
    assert result == tmp_path / "scratchpad" / "label:/some/repo" / "quick-abc"


def test_quick_id_strips_non_hex_characters_before_truncating() -> None:
    environ = {"CLAUDE_CODE_SESSION_ID": "AB-CDEF1234567890"}
    assert paths.quick_id(environ) == "quick-abcdef12"


def test_quick_id_needs_a_real_session_id_value() -> None:
    with pytest.raises(QuickError) as excinfo:
        paths.quick_id({})
    assert str(excinfo.value) == "no session id: a quick swarm is named after the session that starts it as master"


def test_quick_id_accepts_exactly_eight_hex_characters() -> None:
    assert paths.quick_id({"CLAUDE_CODE_SESSION_ID": "ABCDEF12"}) == "quick-abcdef12"


def test_the_fixed_filenames_under_a_quick_folder(tmp_path: Path) -> None:
    assert paths.room(tmp_path) == tmp_path / "room" / "chat.jsonl"
    assert paths.culture(tmp_path) == tmp_path / "culture.md"
    assert paths.learned(tmp_path, "eng-1") == paths.seat_dir(tmp_path, "eng-1") / "learned.md"
    assert paths.opening(tmp_path, "eng-1") == paths.seat_dir(tmp_path, "eng-1") / "opening.md"


def test_read_text_is_empty_when_the_file_is_missing(tmp_path: Path) -> None:
    assert paths.read_text(tmp_path / "missing.txt") == ""


def test_write_index_creates_nested_homes_and_formats_as_indented_json(tmp_path: Path) -> None:
    home_dir = tmp_path / "nested" / "home"
    environ = {"AGENTIHOOKS_HOME": str(home_dir)}
    paths._write_index(environ, {"quick-abc": "/some/folder"})
    written = paths.index_path(environ).read_text()
    assert written == json.dumps({"quick-abc": "/some/folder"}, indent=2)


def test_host_locked_creates_the_nested_home_before_taking_its_lock(tmp_path: Path) -> None:
    home_dir = tmp_path / "nested" / "home"
    environ = {"AGENTIHOOKS_HOME": str(home_dir)}
    with paths.host_locked(environ):
        pass
    assert paths.index_path(environ).parent.is_dir()


def test_quick_exports_joins_each_variable_on_its_own_line() -> None:
    environ = {QUICK_ENV: "quick-abc", SEAT_ENV: "eng-1", DIR_ENV: "/work/q"}
    text = init_agent._quick_exports(environ)
    assert text == (f"export {QUICK_ENV}=quick-abc\nexport {SEAT_ENV}=eng-1\nexport {DIR_ENV}=/work/q\n")
