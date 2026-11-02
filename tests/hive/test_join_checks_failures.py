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


def ledger_probe(monkeypatch, failure):
    from scripts.hive import join_checks
    from scripts.swarm import ledger_client

    ledger = ledger_client._ledger()

    def request(slug, ops, **kwargs):
        if ops[0]["op"] == "ack":
            raise SystemExit(failure)
        return {"_meta": {"rev": 1}}

    monkeypatch.setattr(ledger, "call", request)
    return join_checks.ledger_checks({"slug": "sw", "agent": "engineer", "checks": {}})


def transport_failure(kind):
    import io
    import urllib.error

    if kind == "unreachable":
        return urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    body = b'{"error": "Missing or wrong ledger credential"}' if kind == "credential" else b'{"error": "write refused"}'
    return urllib.error.HTTPError("http://ledger.example/sw", 403, "Forbidden", {}, io.BytesIO(body))


@pytest.mark.parametrize("probe", ["join", "ack"])
def test_refused_unreachable_and_credential_faults_report_distinct_reasons(monkeypatch, probe):
    from scripts.hive import join_checks
    from scripts.swarm import ledger_client

    ledger = ledger_client._ledger()
    name = {"join": "ledger_join", "ack": "ledger_write"}[probe]
    reported = {}
    for kind in ("refused", "unreachable", "credential"):

        def request(slug, ops, service, kind=kind):
            if ops[0]["op"] == probe:
                raise transport_failure(kind)
            return {"_meta": {"rev": 1}}

        monkeypatch.setattr(ledger, "request", request)
        with ledger.ledger_route.scoped("sw", "http://ledger.example"):
            checks = join_checks.ledger_checks({"slug": "sw", "agent": "engineer", "checks": {}})
        assert checks[name] is False
        assert set(checks["errors"]) == {name}
        reported[kind] = checks["errors"][name]
    assert reported["refused"] == {
        "error": "LedgerRefused",
        "status": 403,
        "message": 'ledger sw: server refused: 403 {"error": "write refused"}',
    }
    assert reported["unreachable"] == {
        "error": "LedgerUnreachable",
        "status": None,
        "message": "ledger sw: ledger server not answering on http://ledger.example: "
        "<urlopen error [Errno 111] Connection refused>",
    }
    assert reported["credential"] == {
        "error": "Unauthenticated",
        "status": None,
        "message": "ledger sw: unauthenticated: the ledger server refused the credential",
    }


def test_hive_credential_status_and_value_stay_out_of_the_message(monkeypatch):
    from scripts.hive import join_checks
    from scripts.swarm import ledger_client

    planted = "-".join(("hive", "probe", "value"))
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", planted)
    ledger = ledger_client._ledger()

    def request(slug, ops, **kwargs):
        raise ledger.unauthenticated(f"the ledger server refused the hive credential: 401 {planted}")

    monkeypatch.setattr(ledger, "call", request)
    reported = join_checks.ledger_checks({"slug": "sw", "agent": "engineer", "checks": {}})
    assert reported["errors"]["ledger_join"] == {
        "error": "Unauthenticated",
        "status": 401,
        "message": "ledger sw: unauthenticated: the ledger server refused the hive credential: 401 "
        "[REDACTED:AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL]",
    }


def test_ledger_probe_success_records_no_errors(monkeypatch):
    from scripts.hive import join_checks
    from scripts.swarm import ledger_client

    monkeypatch.setattr(ledger_client._ledger(), "call", lambda slug, ops, **kwargs: {"_meta": {"rev": 1}})
    assert join_checks.ledger_checks({"slug": "sw", "agent": "engineer", "checks": {}}) == {
        "ledger_join": True,
        "ledger_write": True,
    }


def test_ledger_probe_error_message_is_short_and_carries_no_credential(monkeypatch):
    from scripts.hive import join_checks

    token = "ghp_" + "a1B2c3D4e5" * 4
    signed = ".".join(("eyJ" + "a" * 12, "eyJ" + "b" * 12, "c" * 12))
    reported = ledger_probe(monkeypatch, f"server refused: 401 token\n\t{token} {signed}  " + "x" * 400)
    message = reported["errors"]["ledger_write"]["message"]
    assert signed not in message
    assert "[REDACTED:jwt_token]" in message
    assert message.startswith("ledger sw: server refused: 401 ")
    assert "\n" not in message and "\t" not in message
    assert token not in message
    assert "[REDACTED:" in message
    assert len(message) == join_checks.MESSAGE_LIMIT
    assert reported["errors"]["ledger_write"]["status"] == 401


def test_a_falsy_probe_is_a_miss_without_an_exception():
    from scripts.hive import join_checks

    assert join_checks.failure(lambda: False) == {"error": "falsy result", "status": None, "message": ""}
    assert join_checks.failure(lambda: 1) is None
