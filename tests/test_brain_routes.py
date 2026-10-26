import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks.context import brain_routes
from scripts.swarm import brain_binding
from tests import sv2_gat01_cases
from tests.sv2_gat01_cases import Home

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).resolve().parents[1] / "evidence" / "SV2-GAT-01"
PERSONAL = "http://localhost:8104"
RESEARCH = "https://research-brain.example.test"


@pytest.fixture
def inputs():
    return json.loads(sv2_gat01_cases.FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def home(inputs, tmp_path):
    return Home(inputs, tmp_path)


def _refused(environ, reason):
    with pytest.raises(brain_routes.Refused) as raised:
        brain_routes.resolve(environ)
    assert str(raised.value) == reason


def _routes(home, inputs, **changes):
    doc = {**inputs["routes"], **changes}
    home.write_routes(doc)
    return doc


def test_paths_follow_the_agentihooks_home_and_the_routes_variable(tmp_path):
    assert brain_routes.home({"HOME": str(tmp_path)}) == tmp_path / ".agentihooks"
    assert brain_routes.path({"AGENTIHOOKS_HOME": str(tmp_path)}) == tmp_path / "brain-routes.json"
    assert brain_routes.path({brain_routes.FILE_VARIABLE: "~/r.json"}) == Path.home() / "r.json"
    assert brain_routes.store({"AGENTIHOOKS_HOME": str(tmp_path)}) == tmp_path / "brain-routes"


def test_without_a_routes_file_or_swarm_binding_the_legacy_path_is_left_alone(tmp_path):
    environ = {"AGENTIHOOKS_HOME": str(tmp_path), "BRAIN_URL": "https://anything.example.test"}
    assert brain_routes.resolve(environ) is None
    assert brain_routes.apply(environ) is None
    assert environ == {"AGENTIHOOKS_HOME": str(tmp_path), "BRAIN_URL": "https://anything.example.test"}


def test_the_personal_default_routes_with_its_own_credential(home):
    environ = home.environ(KB_ROUTER_TOKEN="fxkb1", BRAIN_HTTP_TOKEN="fxold1")
    receipt = brain_routes.apply(environ)
    assert environ["BRAIN_URL"] == PERSONAL and environ["BRAIN_HTTP_TOKEN"] == "fxpersonal1"
    assert "KB_ROUTER_TOKEN" not in environ
    assert {k: receipt[k] for k in ("status", "route", "origin", "generation")} == {
        "status": "routed",
        "route": "personal",
        "origin": PERSONAL,
        "generation": 1,
    }
    assert json.loads(environ[brain_routes.RECEIPT]) == receipt


def test_an_explicit_route_wins_over_the_default_and_drops_its_trailing_slash(home):
    environ = home.environ(**{brain_routes.VARIABLE: "research"})
    assert brain_routes.apply(environ)["route"] == "research"
    assert environ["BRAIN_URL"] == RESEARCH and environ["BRAIN_HTTP_TOKEN"] == "fxresearch1"


def test_an_allowlisted_session_brain_url_is_replaced_by_the_route(home):
    environ = home.environ(BRAIN_URL=RESEARCH + "/v1")
    assert brain_routes.apply(environ)["route"] == "personal"
    assert environ["BRAIN_URL"] == PERSONAL


def test_a_swarm_session_takes_its_binding_over_an_explicit_route(home, inputs):
    environ = home.swarm(**{brain_routes.VARIABLE: "research"})
    receipt = brain_routes.apply(environ)
    assert (receipt["route"], receipt["origin"], receipt["generation"]) == (
        "swarm",
        inputs["swarm_binding"]["origins"]["lan"]["api"],
        0,
    )
    assert "BRAIN_URL" not in environ


def test_the_swarm_revision_names_its_instance(home):
    environ = home.swarm()
    other = {**environ, brain_binding.VARIABLE: json.dumps({**brain_binding.current(environ), "instance": "other"})}
    assert brain_routes.resolve(environ)["revision"] != brain_routes.resolve(other)["revision"]


def test_a_refused_swarm_binding_refuses_the_brain(home):
    environ = home.environ(**{brain_binding.VARIABLE: json.dumps({"status": "refused", "reason": "r"})})
    _refused(environ, "the swarm brain binding refused this session")


def test_a_repository_brain_url_is_refused_and_counted_without_disclosure(home, inputs):
    environ = home.environ(**inputs["repository_override"], KB_ROUTER_TOKEN="fxkb1")
    receipt = brain_routes.apply(environ)
    assert receipt == {"status": "refused", "reason": brain_routes.UNAUTHORIZED_URL}
    assert json.loads(environ[brain_routes.RECEIPT]) == receipt
    for key in brain_routes.SESSION_KEYS:
        assert key not in environ
    assert environ["BRAIN_ENABLED"] == environ["BRAIN_WRITER_ENABLED"] == "false"
    log = brain_routes.store(environ) / "brain_route_resolution_failures.jsonl"
    assert log.read_text() == json.dumps({"reason": brain_routes.UNAUTHORIZED_URL}) + "\n"
    assert brain_routes.failures(environ) == 1


def test_failures_read_zero_without_a_log_and_counting_survives_an_unwritable_home(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("")
    environ = {"AGENTIHOOKS_HOME": str(blocked)}
    assert brain_routes.failures(environ) == 0
    brain_routes._count(environ, "r")
    assert brain_routes.failures(environ) == 0


@pytest.mark.parametrize("mode", [0o620, 0o602])
def test_a_routes_file_others_can_write_is_refused(home, inputs, mode):
    home.write_routes(inputs["routes"], mode=mode)
    _refused(home.environ(), "the brain routes file is not an operator owned file")


def test_a_routes_symlink_is_refused(home, inputs, tmp_path):
    target = home.write_routes(inputs["routes"])
    link = tmp_path / "link.json"
    link.symlink_to(target)
    _refused(
        home.environ(**{brain_routes.FILE_VARIABLE: str(link)}), "the brain routes file is not an operator owned file"
    )


def test_a_routes_file_owned_by_another_user_is_refused(home, monkeypatch):
    monkeypatch.setattr(brain_routes.os, "getuid", lambda: -1)
    _refused(home.environ(), "the brain routes file is not an operator owned file")


def test_unreadable_or_shapeless_routes_files_are_refused(home):
    found = home.agentihooks / "brain-routes.json"
    found.write_text("{")
    _refused(home.environ(), "the brain routes file is not readable JSON")
    for doc in ([], {"routes": []}, {}):
        found.write_text(json.dumps(doc))
        _refused(home.environ(), "the brain routes file names no routes")


def test_an_unknown_explicit_route_and_a_missing_default_are_refused(home, inputs):
    _refused(home.environ(**{brain_routes.VARIABLE: "attacker"}), "no authorized brain route named attacker")
    _routes(home, inputs, default=None)
    _refused(home.environ(), "no default brain route")
    _routes(home, inputs, default=["personal"])
    _refused(home.environ(), "no default brain route")
    _routes(home, inputs, routes={"personal": "x"})
    _refused(home.environ(), "no default brain route")


@pytest.mark.parametrize(
    "api, allow",
    [
        ("https://unlisted.example.test", [RESEARCH]),
        ("http://research-brain.example.test", ["http://research-brain.example.test"]),
        ("https://u:p@research-brain.example.test", [RESEARCH]),
        ("https://research-brain.example.test?x=1", [RESEARCH]),
        ("https://research-brain.example.test#x", [RESEARCH]),
        ("https://u@research-brain.example.test", [RESEARCH]),
        ("https:///path", ["https://"]),
        (None, [RESEARCH]),
        (RESEARCH, RESEARCH),
    ],
)
def test_a_route_outside_the_allowlist_is_refused(home, inputs, api, allow):
    _routes(home, inputs, allow=allow, routes={"personal": {"api": api, "token_env": "BRAIN_PERSONAL_TOKEN"}})
    _refused(home.environ(), "the route endpoint is not an allowlisted brain origin")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_plain_http_is_allowed_only_on_loopback(home, inputs, host):
    url = f"http://{host}:9"
    _routes(home, inputs, allow=[url], routes={"personal": {"api": url, "token_env": "BRAIN_PERSONAL_TOKEN"}})
    assert brain_routes.resolve(home.environ())["api"] == url


def test_allowlist_origins_compare_case_insensitively(home, inputs):
    _routes(
        home,
        inputs,
        allow=["HTTPS://Research-Brain.example.test"],
        routes={"personal": {"api": RESEARCH, "token_env": "BRAIN_PERSONAL_TOKEN"}},
    )
    assert brain_routes.resolve(home.environ())["api"] == RESEARCH


@pytest.mark.parametrize("reference", [None, "", "lower_case", "BRAIN_HTTP_TOKEN", "KB_ROUTER_TOKEN", "BRAIN_URL", 3])
def test_a_route_must_name_a_dedicated_credential_variable(home, inputs, reference):
    _routes(home, inputs, routes={"personal": {"api": PERSONAL, "token_env": reference}})
    _refused(home.environ(), "route personal names no dedicated credential variable")


def test_an_unset_credential_is_refused(home):
    environ = home.environ(BRAIN_PERSONAL_TOKEN="")
    _refused(environ, "credential BRAIN_PERSONAL_TOKEN is unset")


@pytest.mark.parametrize("revision", ["2", True, 1.0, None])
def test_the_routes_revision_must_be_an_integer(home, inputs, revision):
    _routes(home, inputs, revision=revision)
    _refused(home.environ(), "the brain routes revision is not an integer")


def test_a_routes_file_without_a_revision_is_generation_zero(home, inputs):
    doc = {k: v for k, v in inputs["routes"].items() if k != "revision"}
    home.write_routes(doc)
    assert brain_routes.resolve(home.environ())["generation"] == 0


def test_the_revision_changes_with_the_route_or_the_allowlist(home, inputs):
    first = brain_routes.resolve(home.environ())
    assert first["revision"] == brain_routes.resolve(home.environ())["revision"]
    _routes(home, inputs, allow=[*inputs["routes"]["allow"], "https://more.example.test"])
    assert brain_routes.resolve(home.environ())["revision"] != first["revision"]
    _routes(home, inputs, routes={"personal": {"api": PERSONAL, "token_env": "BRAIN_OTHER_TOKEN"}})
    assert brain_routes.resolve(home.environ(BRAIN_OTHER_TOKEN="fx2"))["revision"] != first["revision"]


def test_the_allowlist_order_does_not_change_the_revision(home, inputs):
    first = brain_routes.resolve(home.environ())["revision"]
    _routes(home, inputs, allow=list(reversed(inputs["routes"]["allow"])))
    assert brain_routes.resolve(home.environ())["revision"] == first


def _remembered(environ, session="s1"):
    return json.loads((brain_routes.store(environ) / "sessions" / f"{session}.json").read_text())


def test_remember_without_a_receipt_or_with_an_unsafe_session_writes_nothing(home):
    environ = home.environ()
    assert brain_routes.remember(environ, "s1") is None
    brain_routes.apply(environ)
    assert brain_routes.remember(environ, "../escape") is None
    assert brain_routes.remember(environ, "") is None
    assert not brain_routes.store(environ).exists()


def test_remember_tracks_fresh_same_invalidated_and_stale(home, inputs):
    first = home.environ()
    brain_routes.apply(first)
    old = json.loads(first[brain_routes.RECEIPT])
    assert brain_routes.remember(first, "s1") == "fresh"
    assert _remembered(first) == old
    cached = brain_routes.cache(first, "s1", old)
    assert cached == brain_routes.store(first) / "cache" / "s1" / old["revision"]
    cached.mkdir(parents=True)
    assert brain_routes.remember(first, "s1") == "same"
    assert cached.exists()
    home.write_routes(inputs["changed_routes"])
    second = home.environ()
    brain_routes.apply(second)
    assert brain_routes.remember(second, "s1") == "invalidated"
    assert not cached.exists()
    assert brain_routes.remember(first, "s1") == "stale"
    assert _remembered(first) == json.loads(second[brain_routes.RECEIPT])


def test_a_refused_receipt_replaces_a_routed_one(home, inputs):
    first = home.environ()
    brain_routes.apply(first)
    brain_routes.remember(first, "s1")
    refused = home.environ(**inputs["repository_override"])
    brain_routes.apply(refused)
    assert brain_routes.remember(refused, "s1") == "invalidated"
    assert _remembered(first) == {"status": "refused", "reason": brain_routes.UNAUTHORIZED_URL}


def test_a_corrupt_stored_receipt_is_treated_as_fresh(home):
    environ = home.environ()
    brain_routes.apply(environ)
    found = brain_routes.store(environ) / "sessions" / "s1.json"
    found.parent.mkdir(parents=True)
    found.write_text("{")
    assert brain_routes.remember(environ, "s1") == "fresh"


def test_session_start_remembers_the_route_before_refreshing(home, monkeypatch):
    from hooks import config
    from hooks.context import brain_adapter

    environ = home.environ()
    brain_routes.apply(environ)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    order = []
    monkeypatch.setattr(config, "BRAIN_ENABLED", True)
    monkeypatch.setattr(
        brain_routes, "remember", lambda env, session: order.append(("remember", env[brain_routes.RECEIPT], session))
    )
    monkeypatch.setattr(brain_adapter, "force_refresh", lambda: order.append(("refresh",)) or True)
    monkeypatch.setattr("hooks.context.project_identity.resolve_project", Mock(return_value=None))
    monkeypatch.setattr("hooks.context.project_sessions.record_session", Mock())
    monkeypatch.setattr("hooks.context.project_cache.project_context", Mock(return_value=None))
    assert brain_adapter.inject_on_session_start("s1", "") is True
    assert order == [("remember", environ[brain_routes.RECEIPT], "s1"), ("refresh",)]
    order.clear()
    assert brain_adapter.inject_on_session_start("", "") is True
    assert order == [("refresh",)]


@pytest.fixture
def hook_env(home, monkeypatch):
    from hooks import config

    for key in (
        "BRAIN_URL",
        "BRAIN_HTTP_TOKEN",
        "KB_ROUTER_TOKEN",
        "BRAIN_ENABLED",
        "BRAIN_WRITER_ENABLED",
        "BRAIN_SOURCE_TYPE",
        brain_binding.VARIABLE,
        brain_routes.VARIABLE,
        brain_routes.RECEIPT,
        brain_routes.FILE_VARIABLE,
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in home.environ(AGENTIBRAIN_HOME=str(home.root / ".agentibrain")).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config, "_BRAIN_FILE_OWNED", set())
    monkeypatch.setattr(config, "_FILE_OWNED_KEYS", set())
    for name in (
        "BRAIN_ENABLED",
        "BRAIN_SOURCE_TYPE",
        "BRAIN_SOURCE_PATH",
        "BRAIN_CHANNEL",
        "BRAIN_REFRESH_TOOL_CALLS",
        "BRAIN_URL",
        "BRAIN_HTTP_TOKEN",
        "BRAIN_HTTP_TIMEOUT",
        "BRAIN_WRITER_ENABLED",
        "AMYGDALA_ENABLED",
        "AMYGDALA_SIGNAL_PATH",
        "_ENV_FINGERPRINT",
    ):
        monkeypatch.setattr(config, name, getattr(config, name))
    return config


def test_a_config_reload_routes_the_session_brain(hook_env):
    resolved = hook_env.reload_brain_env(force=True)
    assert (resolved["brain_url"], resolved["brain_enabled"]) == (PERSONAL, True)
    assert hook_env.BRAIN_HTTP_TOKEN == "fxpersonal1"


def test_a_config_reload_refuses_a_repository_brain_url(hook_env, inputs, monkeypatch):
    for key, value in inputs["repository_override"].items():
        monkeypatch.setenv(key, value)
    resolved = hook_env.reload_brain_env(force=True)
    assert (resolved["brain_url"], resolved["brain_enabled"], resolved["brain_writer_enabled"]) == ("", False, False)
    assert hook_env.BRAIN_HTTP_TOKEN == ""


def test_config_routes_the_brain_after_the_swarm_guard(hook_env, monkeypatch):
    calls = []
    monkeypatch.setattr(hook_env, "_guard_swarm_brain", lambda: calls.append("guard"))
    monkeypatch.setattr(brain_routes, "apply", lambda environ: calls.append(("apply", environ is hook_env.os.environ)))
    hook_env._route_brain()
    assert calls == ["guard", ("apply", True)]


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_gat01_cases.run_case(case), sv2_gat01_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)


def test_a_failing_case_keeps_its_reproduction(monkeypatch):
    def failing(inputs, root):
        assert root is None

    monkeypatch.setitem(sv2_gat01_cases.CASES, "c", failing)
    record = sv2_gat01_cases.run_case("c")
    assert record["state"] == "failed" and "observed" not in record
    assert record["reproduce"] == {
        "fixture": "tests/fixtures/swarm_v2/brain-routes.json",
        "case": "c",
        "line": failing.__code__.co_firstlineno + 1,
        "assertion": "assert root is None",
    }


def test_the_package_result_names_the_case_records_and_states():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, value in sv2_gat01_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == value
    assert manifest["cases"] == ["T-SV2-GAT-01-A", "T-SV2-GAT-01-B", "T-SV2-GAT-01-C"]
    assert result["package"] == "SV2-GAT-01"
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    assert set(result["metrics"]) == {brain_routes.FAILURES}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
