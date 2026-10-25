import json
from dataclasses import replace

import pytest

from scripts.swarm import execution
from scripts.swarm.store import APP_SERVER_TRANSPORT, LEGACY_TRANSPORT, AgentRecord, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.unit


def test_codex_inbox_transport_defaults_to_app_server(env):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert store.config("sw").codex_inbox_transport == APP_SERVER_TRANSPORT


def test_a_stored_config_without_the_setting_keeps_legacy(env):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.redis.hdel(store.key("sw", "config"), "codex_inbox_transport")
    assert store.config("sw").codex_inbox_transport == LEGACY_TRANSPORT


def test_rollback_to_legacy_holds_for_new_executions_and_keeps_admitted_ones(env):  # noqa: F811
    from scripts.swarm_v2.runtime import codex

    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    agent = AgentRecord(store.next_name("sw", "eng"), "eng", "task", seat="eng-1@sw", harness="codex")
    admitted = store.start_execution("sw", replace(agent, transport=codex.transport(store.config("sw"), "codex")))
    assert run("sw", "set", "codex-inbox-transport=legacy") == 0
    assert codex.transport(store.config("sw"), "codex") == LEGACY_TRANSPORT
    assert store.execution("sw", admitted.execution_id).transport == APP_SERVER_TRANSPORT


def test_set_takes_a_string_transport_and_reports_it(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    capsys.readouterr()
    assert run("sw", "set", "codex-inbox-transport=app_server") == 0
    assert store.config("sw").codex_inbox_transport == APP_SERVER_TRANSPORT
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["codex_inbox_transport"] == "app_server"
    assert run("sw", "set", "codex-inbox-transport=legacy") == 0
    assert store.config("sw").codex_inbox_transport == LEGACY_TRANSPORT


def test_set_refuses_an_unknown_transport(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    before = store.config("sw")
    capsys.readouterr()
    assert run("sw", "set", "codex-inbox-transport=pane") == 1
    assert capsys.readouterr().err.strip() == "swarm: codex inbox transport must be one of legacy, app_server"
    assert store.config("sw") == before


def test_an_admitted_execution_keeps_its_transport_after_the_setting_changes(env):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    name = store.next_name("sw", "eng")
    agent = AgentRecord(name, "eng", "task", seat="eng-1@sw", harness="codex", transport=APP_SERVER_TRANSPORT)
    admitted = store.start_execution("sw", agent)
    assert store.execution("sw", admitted.execution_id).transport == APP_SERVER_TRANSPORT
    run("sw", "set", "codex-inbox-transport=legacy")
    with pytest.raises(SwarmError, match="stale or changed execution identity"):
        store.put_agent("sw", replace(admitted, transport=LEGACY_TRANSPORT))
    assert store.execution("sw", admitted.execution_id).transport == APP_SERVER_TRANSPORT


def test_the_transport_stays_off_the_agent_projection_and_comes_back_from_the_execution(env):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    agent = AgentRecord(store.next_name("sw", "eng"), "eng", "task", seat="eng-1@sw", transport=APP_SERVER_TRANSPORT)
    admitted = store.start_execution("sw", agent)
    raw = json.loads(store.redis.hget(store.key("sw", "agents"), admitted.name))
    assert "transport" not in raw
    assert [found.transport for found in execution.ExecutionRegistry(store).agents("sw")] == [APP_SERVER_TRANSPORT]


def test_an_execution_recorded_before_the_transport_field_reads_back_with_no_transport(env):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    agent = AgentRecord(store.next_name("sw", "eng"), "eng", "task", seat="eng-1@sw", transport=APP_SERVER_TRANSPORT)
    admitted = store.start_execution("sw", agent)
    executions = store.key("sw", "executions")
    row = json.loads(store.redis.hget(executions, admitted.execution_id))
    del row["transport"]
    store.redis.hset(executions, admitted.execution_id, json.dumps(row))
    assert [found.transport for found in execution.ExecutionRegistry(store).agents("sw")] == [""]
