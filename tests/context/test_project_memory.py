from datetime import datetime, timezone
from unittest.mock import patch

from hooks.context.brain_adapter import BrainEntry
from hooks.context.project_identity import ProjectIdentity
from hooks.context.project_memory import VaultProjectSource
from hooks.context.project_sessions import record_session


class _UtcClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 5, 23, 30, tzinfo=timezone.utc).astimezone(tz)


def test_source_confirms_project_and_attributes_lessons(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setenv("BRAIN_STALE_LESSON_DAYS", "1")
    monkeypatch.setattr("hooks.context.project_memory.datetime", _UtcClock)
    identity = ProjectIdentity("alpha", "org/alpha")
    record_session("ours", identity)
    record_session("theirs", ProjectIdentity("beta", "org/beta"))
    entries = [
        BrainEntry(
            "hot-arcs-today",
            "Active Hot Arcs",
            "| a | 9 | pineal | active | Alpha |\n| b | 10 | pineal | active | Beta |",
        )
    ]
    responses = {
        "cluster_id: a": {"results": [{"path": "pineal/a.md"}]},
        "cluster_id: b": {"results": [{"path": "pineal/b.md"}]},
        "pineal/a.md": {
            "content": "---\ncluster_id: a\nproject: alpha\ntitle: Alpha\nsummary: Alpha focus\nstatus: active\nupdated: 2026-10-05\n---\n"
        },
        "pineal/b.md": {"content": "---\ncluster_id: b\nproject: beta\ntitle: Beta\n---\n"},
        "left/reference/lessons-2026-10-05.md": {
            "content": "## now — agent — `ours`\n\nOur lesson.\n\n## now — agent — `theirs`\n\nOther lesson.\n"
        },
    }
    with patch(
        "hooks._brain_http.get",
        side_effect=lambda path, params=None: responses.get(params.get("path", params.get("q"))),
    ):
        memory = VaultProjectSource(entries).fetch(identity)
    assert [arc["id"] for arc in memory.arcs] == ["a"]
    assert memory.lessons == ["Our lesson."]
    assert memory.arcs[0]["summary"] == "Alpha focus"


def test_scratch_project_receives_its_git_session_lessons(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setenv("BRAIN_STALE_LESSON_DAYS", "1")
    record_session("git", ProjectIdentity("alpha", "alpha", remote="org/alpha"))
    with patch("hooks._brain_http.get", return_value={"content": "## now — agent — `git`\n\nGit lesson.\n"}):
        memory = VaultProjectSource([]).fetch(ProjectIdentity("alpha", "alpha"))
    assert memory.lessons == ["Git lesson."]
