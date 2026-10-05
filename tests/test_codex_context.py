import json

from scripts.codex_context import CodexContext, codex_context


def _token_count(last: int, window: int, total: int = 1_200_000) -> str:
    info = {
        "total_token_usage": {"total_tokens": total},
        "last_token_usage": {"total_tokens": last},
        "model_context_window": window,
    }
    return json.dumps(
        {"timestamp": "2026-10-04T10:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": info}}
    )


def test_latest_token_count_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    day = tmp_path / "sessions" / "2026" / "10" / "04"
    day.mkdir(parents=True)
    lines = [_token_count(100, 272000), '{"type": "event_msg"}', _token_count(5000, 272000), "not json"]
    (day / "rollout-2026-10-04T10-00-00-abc123.jsonl").write_text("\n".join(lines) + "\n")
    assert codex_context("abc123") == CodexContext(used=5000, window=272000)


def test_context_is_the_last_turn_not_the_session_total(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    day = tmp_path / "sessions" / "2026" / "10" / "05"
    day.mkdir(parents=True)
    (day / "rollout-2026-10-05T13-05-57-def456.jsonl").write_text(_token_count(95587, 828400, total=1206280) + "\n")
    assert codex_context("def456") == CodexContext(used=95587, window=828400)


def test_missing_rollout_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert codex_context("nosuchsession") is None
