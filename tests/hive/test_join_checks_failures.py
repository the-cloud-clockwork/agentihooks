from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("failed", [None, "join", "ack"])
def test_startup_observes_ledger_join_and_write_independently(monkeypatch, failed, tmp_path):
    from fakeredis import FakeRedis

    from scripts.hive import join_checks
    from scripts.swarm import ledger_client

    ledger = ledger_client._ledger()
    calls = []

    def request(slug, ops, **kwargs):
        calls.append((slug, [{key: value for key, value in op.items() if key != "id"} for op in ops]))
        return (
            {"rejected": True, "_meta": {"warnings": ["refused"]}} if ops[0]["op"] == failed else {"_meta": {"rev": 1}}
        )

    monkeypatch.setattr(ledger, "call", request)
    monkeypatch.setattr(join_checks.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    context = {
        "home": str(tmp_path / "missing"),
        "profile": "engineer",
        "harness": "claude",
        "pane": "",
        "transcript_dir": "",
    }
    report = {"agent": "engineer", "slug": "sw", "execution_id": "execution", "started_at": 1000, "context": context}
    checks = join_checks.startup(FakeRedis(decode_responses=True), report)
    assert checks["ledger_join"] is (failed != "join")
    assert checks["ledger_write"] is (failed != "ack")
    assert checks["github"] is True
    assert checks["brain"] is False
    assert calls == [
        ("sw", [{"op": "join", "by": "engineer", "role": "member"}]),
        ("sw", [{"op": "ack", "by": "engineer", "rev": 0}]),
    ]


def test_cheap_checks_record_missing_local_capabilities(tmp_path):
    from fakeredis import FakeRedis

    from scripts.hive import join_checks

    context = {
        "home": str(tmp_path / "missing"),
        "profile": "engineer",
        "harness": "claude",
        "pane": "",
        "transcript_dir": "",
    }
    checks = join_checks.cheap(
        FakeRedis(decode_responses=True),
        {"slug": "sw", "execution_id": "execution", "started_at": 0, "context": context},
    )
    assert checks == {
        "redis_claim": True,
        "redis_release": True,
        "redis_status": True,
        "redis_verdict": True,
        "transcript": False,
        "profile": False,
        "settings": False,
        "pane": False,
        "started_at": False,
        "brain": False,
    }


@pytest.mark.parametrize("error", [FileNotFoundError(), TimeoutError()])
def test_unavailable_github_cli_is_a_failed_required_check(monkeypatch, error):
    from scripts.hive import join_checks

    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(join_checks.subprocess, "run", run)
    assert join_checks.github_ready() is False
