from unittest.mock import patch

import pytest

from hooks.context import broadcast, project_identity, project_sessions


@pytest.mark.parametrize("reader", ["get_pending_broadcasts", "get_unseen_broadcasts", "get_pretool_broadcasts"])
def test_strict_session_filters_only_fleet_memory(monkeypatch, tmp_path, reader):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(broadcast, "_broadcast_path", lambda: tmp_path / "broadcast.json")
    monkeypatch.setattr(broadcast, "_sessions_path", lambda: tmp_path / "sessions.json")
    monkeypatch.setenv("BRAIN_PROJECT_SCOPE", "strict")
    monkeypatch.setattr(broadcast, "BASE_CHANNELS", ["brain"])
    with patch(
        "hooks.context.project_identity.resolve_project",
        side_effect=[
            __import__("hooks.context.project_identity", fromlist=["ProjectIdentity"]).ProjectIdentity(
                "alpha", "alpha"
            ),
            None,
        ],
    ):
        broadcast.register_session("owned", 0, "/repo/alpha", "")
        broadcast.register_session("unowned", 0, "/home", "")
    keys = ("hot-arcs-today", "lessons", "operator-intent", "signals", "inject", "last-tick-diff", "amygdala-active")
    broadcast.reconcile_channel_broadcasts(
        "brain",
        [{"message": key, "source": "brain-adapter", "origin": {"id": key}, "persistent": True} for key in keys]
        + [{"message": "ordinary", "origin": {"id": "lessons"}}],
    )
    get = getattr(broadcast, reader)
    owned = {m["message"] for m in get("owned")}
    assert owned == {"signals", "inject", "last-tick-diff", "amygdala-active", "ordinary"}
    assert "hot-arcs-today" in {m["message"] for m in get("unowned")}
    monkeypatch.setenv("BRAIN_PROJECT_SCOPE", "rank")
    assert "hot-arcs-today" in {m["message"] for m in get("owned")}
    monkeypatch.setenv("BRAIN_PROJECT_SCOPE", "off")
    assert "lessons" in {m["message"] for m in get("owned")}


def test_project_sessions_keeps_the_real_resolver():
    assert project_sessions.resolve_project is project_identity.resolve_project
