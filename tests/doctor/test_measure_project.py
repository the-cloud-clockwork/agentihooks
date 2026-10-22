import httpx
import pytest

from scripts import profile_telemetry
from scripts.doctor import cli as doctor
from scripts.doctor import detect, loop, traces_read
from scripts.swarm.store import AgentRecord
from tests.doctor.test_doctor_cli import DOCTOR, WATCHED, env  # noqa: F401
from tests.doctor.test_measure_cli import LIVE, NOW, measured, started, telemetry  # noqa: F401

INSTALLED = {"OTEL_LANGFUSE_PUBLIC_KEY": "installed-public", "OTEL_LANGFUSE_SECRET_KEY": "installed-secret"}
CALLER = {"LANGFUSE_PUBLIC_KEY": "caller-public", "LANGFUSE_SECRET_KEY": "caller-secret", "LANGFUSE_HOST": "http://lf"}
INSTALLED_KEYS = ("installed-public", "installed-secret")
CALLER_KEYS = ("caller-public", "caller-secret")
SWARM = [{"id": "p-swarm", "name": "agent-swarm"}]
ANTONCORE = [{"id": "p-core", "name": "antoncore"}]
STALE_TRACE = {"id": "old", "name": "s-eng-1", "sessionId": "c1", "tags": [f"swarm:{WATCHED}", "agent:old"]}
WRONG = "the installed Langfuse keys serve project antoncore, not the watched swarm project agent-swarm"


def installed(monkeypatch, keys=INSTALLED):
    monkeypatch.setattr(profile_telemetry, "agentihooks_langfuse_keys", lambda: dict(keys))


def serving(monkeypatch, projects):
    asked = []

    def client(environ):
        keys = (environ.get("LANGFUSE_PUBLIC_KEY"), environ.get("LANGFUSE_SECRET_KEY"))

        def get(path, params):
            asked.append((keys, path))
            project = projects[keys]
            if isinstance(project, Exception):
                raise project
            if path == "projects":
                return {"data": project}
            return {"data": [STALE_TRACE], "meta": {"page": 1, "totalPages": 1, "totalItems": 1}}

        return get

    monkeypatch.setattr(traces_read, "client", client)
    return asked


def test_the_installed_keys_replace_the_caller_keys_and_keep_the_host(monkeypatch):
    installed(monkeypatch)
    assert traces_read.installed_env(CALLER) == {
        "LANGFUSE_HOST": "http://lf",
        "LANGFUSE_PUBLIC_KEY": "installed-public",
        "LANGFUSE_SECRET_KEY": "installed-secret",
    }


def test_the_confirmed_project_reads_with_the_installed_keys_only(monkeypatch):
    installed(monkeypatch)
    asked = serving(monkeypatch, {INSTALLED_KEYS: SWARM, CALLER_KEYS: ANTONCORE})
    get = traces_read.measured_client(CALLER)
    assert get("traces", {"tags": f"swarm:{WATCHED}"})["data"] == [STALE_TRACE]
    assert asked == [(INSTALLED_KEYS, "projects"), (INSTALLED_KEYS, "traces")]


def test_missing_installed_keys_never_fall_back_to_the_caller_keys(monkeypatch):
    installed(monkeypatch, {})
    assert traces_read.installed_env(CALLER) == {"LANGFUSE_HOST": "http://lf"}
    with pytest.raises(RuntimeError) as refused:
        traces_read.measured_client(CALLER)
    assert str(refused.value) == (
        "Langfuse project identity is unavailable: RuntimeError: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set"
    )


def test_a_nonempty_wrong_project_with_stale_watched_tags_is_refused_before_any_trace_read(monkeypatch):
    installed(monkeypatch)
    asked = serving(monkeypatch, {INSTALLED_KEYS: ANTONCORE})
    with pytest.raises(RuntimeError) as refused:
        traces_read.measured_client(CALLER)
    assert str(refused.value) == WRONG
    assert asked == [(INSTALLED_KEYS, "projects")]


def test_two_projects_behind_one_key_are_refused(monkeypatch):
    installed(monkeypatch)
    serving(monkeypatch, {INSTALLED_KEYS: [*SWARM, *ANTONCORE]})
    with pytest.raises(RuntimeError) as refused:
        traces_read.measured_client(CALLER)
    assert str(refused.value) == (
        "the installed Langfuse keys serve project agent-swarm, antoncore, not the watched swarm project agent-swarm"
    )


def test_a_missing_project_identity_is_refused(monkeypatch):
    installed(monkeypatch)
    serving(monkeypatch, {INSTALLED_KEYS: []})
    with pytest.raises(RuntimeError) as refused:
        traces_read.measured_client(CALLER)
    assert str(refused.value) == "Langfuse project identity is unavailable: the installed keys name no project"


def test_a_project_without_a_name_is_a_missing_identity(monkeypatch):
    installed(monkeypatch)
    serving(monkeypatch, {INSTALLED_KEYS: [{"id": "p-swarm"}]})
    with pytest.raises(RuntimeError) as refused:
        traces_read.measured_client(CALLER)
    assert str(refused.value) == "Langfuse project identity is unavailable: the installed keys name no project"


def test_a_failed_project_read_is_refused_naming_the_error(monkeypatch):
    installed(monkeypatch)
    serving(monkeypatch, {INSTALLED_KEYS: httpx.ConnectError("refused")})
    with pytest.raises(RuntimeError) as refused:
        traces_read.measured_client(CALLER)
    assert str(refused.value) == "Langfuse project identity is unavailable: ConnectError: refused"


def test_the_trace_detector_reads_through_the_client_it_is_given(monkeypatch):
    given = []

    def refusing(environ):
        given.append(environ)
        raise RuntimeError(WRONG)

    monkeypatch.setattr(traces_read, "client", lambda environ: pytest.fail("the default client was used"))
    store = type("Store", (), {"redis": object(), "agents": lambda self, slug: []})()
    ledger = type("Ledger", (), {"tasks": lambda self, slug: []})()
    reader = detect.readers(store, ledger, "s", 1, environ=CALLER, telemetry=refusing)["trace"]
    with pytest.raises(RuntimeError, match=WRONG):
        reader()
    assert given == [CALLER]


def detecting(monkeypatch):
    def readers(store, ledger, slug, now, telemetry):
        assert (slug, now, telemetry) == (WATCHED, NOW, traces_read.measured_client)
        return {"trace": lambda: [] if telemetry(dict(CALLER)) else None}

    monkeypatch.setattr(doctor.detect, "readers", readers)


def gone_finding(store):
    store.put_agent(WATCHED, AgentRecord(name=LIVE, lane="eng", task="t1"))
    gone = telemetry(LIVE, 600)
    loop.record(store, DOCTOR, [gone], NOW - 60_000, 0)
    return gone


def test_measure_refuses_a_zero_from_the_wrong_project(started, monkeypatch, capsys):  # noqa: F811
    gone = gone_finding(started)
    installed(monkeypatch)
    serving(monkeypatch, {INSTALLED_KEYS: ANTONCORE, CALLER_KEYS: ANTONCORE})
    detecting(monkeypatch)
    code, out, err = measured(capsys, gone.id)
    assert (code, out) == (1, "")
    assert err.splitlines() == [
        f"the trace detector failed: RuntimeError: {WRONG}",
        f"doctor: {gone.id} is unknown because the trace detector failed: RuntimeError: {WRONG}",
    ]


def test_measure_reads_a_zero_from_the_confirmed_project(started, monkeypatch, capsys):  # noqa: F811
    gone = gone_finding(started)
    installed(monkeypatch)
    serving(monkeypatch, {INSTALLED_KEYS: SWARM, CALLER_KEYS: ANTONCORE})
    detecting(monkeypatch)
    assert measured(capsys, gone.id) == (0, f"{gone.id} 0\n", "")


def test_a_class_read_from_a_missing_project_identity_is_refused(started, monkeypatch, capsys):  # noqa: F811
    gone_finding(started)
    installed(monkeypatch)
    serving(monkeypatch, {INSTALLED_KEYS: []})
    detecting(monkeypatch)
    code, out, err = measured(capsys, "telemetry-stale")
    assert (code, out) == (1, "")
    assert err == (
        "doctor: telemetry-stale is unknown: the trace detector failed: RuntimeError: "
        "Langfuse project identity is unavailable: the installed keys name no project\n"
    )
