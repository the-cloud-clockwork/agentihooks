from pathlib import Path

from hooks.context.project_identity import ProjectIdentity
from hooks.context.project_sessions import lookup, record_session


def test_persistent_identity_survives_registry_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    identity = ProjectIdentity("alpha", "org/alpha", "fix", "/work/alpha/fix")
    record_session("session", identity)
    assert lookup("session") == identity
    assert lookup("absent") is None
    record_session("session", identity)
    assert len((tmp_path / "brain" / "project-sessions.jsonl").read_text().splitlines()) == 1


def test_lookup_returns_the_stored_project_id(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    record_session("scoped", ProjectIdentity("alpha", "org/alpha", project_id="github:org/alpha"))
    assert lookup("scoped").project_id == "github:org/alpha"
    index = tmp_path / "brain" / "project-sessions.jsonl"
    index.write_text(index.read_text() + '{"session_id": "legacy", "project": "beta", "repo": "org/beta"}\n')
    assert lookup("legacy").project_id == "unknown"


def test_old_session_uses_transcript_folder(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / "state")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    transcript = tmp_path / ".claude" / "projects" / str(repo).replace("/", "-") / "old.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("")
    monkeypatch.setattr(
        "hooks.context.project_identity._git",
        lambda path, *args: (
            str(repo / ".git") if "--git-common-dir" in args else str(repo) if "--show-toplevel" in args else ""
        ),
    )
    assert lookup("old").project == "repo"
