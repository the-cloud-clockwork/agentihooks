from pathlib import Path

import pytest

from hooks.context import conditions, master_coding

SID = "master-session"


@pytest.fixture
def master(tmp_path, monkeypatch):
    for key, value in {"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_SWARM_TASK": "master"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    return tmp_path


@pytest.mark.parametrize("signal", ["deny-master-coding", "disallow-master-coding"])
def test_the_window_lasts_until_typed_denial(master, monkeypatch, signal):
    assert master_coding.observe(SID, "allow-master-coding", typed=True, now=1000) == "open"
    monkeypatch.setattr(master_coding.time, "time", lambda: 100000)
    assert master_coding.is_open(SID)
    assert master_coding.observe(SID, signal, typed=False) == ""
    assert master_coding.is_open(SID)
    assert master_coding.observe(SID, signal, typed=True) == "closed"
    assert not master_coding.is_open(SID)


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact", None, "unknown"])
def test_session_start_revokes_the_window_except_during_compaction(master, monkeypatch, source):
    from hooks import hook_manager

    def stop():
        raise RuntimeError("session event reached")

    master_coding.observe(SID, "allow-master-coding", typed=True)
    monkeypatch.setattr(hook_manager, "_inject_refocus", lambda *args: None)
    monkeypatch.setattr("hooks.lifecycle.guard.session_event", stop)
    with pytest.raises(RuntimeError, match="^session event reached$"):
        hook_manager.on_session_start({"session_id": SID, "source": source})
    assert master_coding.is_open(SID) is (source == "compact")


def test_close_is_session_scoped_and_idempotent(master):
    master_coding.observe(SID, "allow-master-coding", typed=True)
    for session in (None, "", "other"):
        master_coding.close(session)
        assert master_coding.is_open(SID)
    assert not master_coding.is_open("")
    master_coding.close(SID)
    master_coding.close(SID)
    assert not master_coding.is_open(SID)


def test_a_window_close_failure_is_logged(master, monkeypatch):
    from unittest.mock import Mock

    from hooks import hook_manager

    close = Mock(side_effect=RuntimeError("close failed"))
    logged = Mock()
    monkeypatch.setattr(master_coding, "close", close)
    monkeypatch.setattr(hook_manager, "log", logged)
    hook_manager._close_master_coding(SID, "startup")
    close.assert_called_once_with(SID)
    logged.assert_called_once_with("master coding window close failed", {"error": "close failed"})


def test_reopening_an_open_window_refreshes_it(master):
    assert master_coding.observe(SID, "allow-master-coding", typed=True, now=1000) == "open"
    assert master_coding.observe(SID, "allow-master-coding", typed=True, now=2000) == "open"
    assert master_coding.is_open(SID)


def test_an_open_record_is_ignored_once_the_session_is_no_longer_the_master(master, monkeypatch):
    master_coding.observe(SID, "allow-master-coding", typed=True, now=1000)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t12")
    assert not master_coding.is_open(SID)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "master")
    assert master_coding.is_open(SID)
    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    assert not master_coding.is_open(SID)


def test_a_window_import_failure_denies_edits_through_the_condition_runner(master, monkeypatch):
    guard = Path(__file__).resolve().parents[1] / "profiles/package/roles/_guards/no_code_edits.py"
    condition = master / "pre-edit-window.py"
    condition.write_text(
        "import runpy\nimport sys\n"
        "sys.modules['hooks.context.master_coding'] = None\n"
        f"raise SystemExit(runpy.run_path({str(guard)!r})['main']('master'))\n"
    )
    entry = {"file": condition.name, "path": str(condition), "ext": ".py", "async": False, "source": "project"}
    monkeypatch.setattr("hooks.config.CONDITIONS_ENABLED", True)
    monkeypatch.setattr(conditions, "matching", lambda *args, **kwargs: [entry])
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(master / "repo" / "x.py")},
        "session_id": SID,
        "cwd": str(guard.parents[4]),
    }
    effect = conditions.pre_effect(payload)
    assert effect.block == (
        "[condition pre-edit-window.py] master cannot evaluate the coding window; edits stay closed."
    )
    assert effect.contexts == []


@pytest.mark.parametrize(
    ("name", "closed"),
    [
        ("hooks/x.py", False),
        (".claude/conditions/pre-bash-x.py", True),
        (".env", True),
        ("stack.env", True),
        (".env.production", True),
        (".netrc", True),
        (".npmrc", True),
        (".pypirc", True),
        (".git-credentials", True),
        (".pgpass", True),
        (".env.example", False),
        (".env.sample", False),
        (".env.template", False),
        (".env.dist", False),
    ],
)
def test_conditions_and_credentials_stay_closed_inside_the_window(tmp_path, name, closed):
    assert master_coding.closed_in_window(name, tmp_path) is closed
    assert master_coding.closed_in_window(str(tmp_path / name), Path("/")) is closed
