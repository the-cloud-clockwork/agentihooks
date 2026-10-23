import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.codex_session import protocol, supervisor
from scripts.codex_session.binding import CONNECTED, ENDPOINTS, EXITED, FAILED, BindingStore, decode
from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.codex_session.fakes import FIRST_PID, OWN_START, SERVER_START, Clock, Process, Server, hooks

pytestmark = pytest.mark.unit

SLUG = "fixture"
SEAT = f"eng-1@{SLUG}"


import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "agentihooks", 1, 0, codex_inbox_transport=APP_SERVER_TRANSPORT))
    return store


@pytest.fixture
def root():
    path = Path(tempfile.mkdtemp(prefix="cx-"))
    yield path / "cx"
    shutil.rmtree(path)


@pytest.fixture
def server(tmp_path):
    return Server(str(tmp_path / "codex-home"))


@pytest.fixture
def clock():
    return Clock()


def deps(server, clock, root, **changes):
    built = supervisor.Deps(
        popen=server.popen,
        connect=server.connect,
        start_identity=server.start_identity,
        clock=clock,
        sleep=clock.sleep,
        now_ms=lambda: 1000,
        runtime_root=root,
    )
    return replace(built, **changes)


def launch_spec(store, server, tmp_path, name="", **changes):
    agent = AgentRecord(
        name or store.next_name(SLUG, "eng"),
        "eng",
        "task",
        seat=SEAT,
        account="default",
        profile="engineer",
        model="gpt-6.1-sol",
        effort="high",
    )
    spec = supervisor.Launch(
        slug=SLUG,
        agent=agent,
        cwd=str(tmp_path),
        codex_home=server.codex_home,
        environ={"CODEX_HOME": server.codex_home, "AGENTIHOOKS_SWARM": SLUG},
        opening="Start the task.",
        account_type="chatgpt",
        bundle_revision="rev-1",
        startup_timeout_s=2.0,
    )
    return replace(spec, **changes)


def other_seat(spec, seat="eng-2"):
    return replace(spec, agent=replace(spec.agent, seat=f"{seat}@{SLUG}"))


def failed(store, server, clock, root, spec, error_class, **dep_changes):
    with pytest.raises(supervisor.LaunchFailure) as failure:
        supervisor.launch(store, spec, deps(server, clock, root, **dep_changes))
    assert failure.value.error_class == error_class
    return failure.value


def only_record(store):
    rows = store.redis.hvals(store.key(SLUG, "codex-transport"))
    assert len(rows) == 1
    return decode(rows[0])


def assert_failed_cleanly(store, server, error_class):
    record = only_record(store)
    assert (record.state, record.last_error_class) == (FAILED, error_class)
    assert record.thread_id == ""
    assert server.processes[-1].poll() is not None
    assert store.redis.hgetall(store.key(SLUG, ENDPOINTS)) == {}
    assert "turn/start" not in server.methods()


def test_a_launch_admits_one_server_binds_its_thread_and_opens_once(store, server, clock, root, tmp_path):
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    record, agent = started.record, started.agent
    path = Path(record.endpoint_ref.removeprefix("unix://"))
    assert path == root / agent.execution_id.removeprefix("exe-")[:16] / "s"
    assert server.commands == [["codex", "app-server", "--listen", f"unix://{path}"]]
    assert server.spawns == [
        {
            "env": {"CODEX_HOME": str(path.parent / "codex"), "AGENTIHOOKS_SWARM": SLUG},
            "cwd": str(tmp_path),
            "stdin": subprocess.DEVNULL,
            "start_new_session": True,
        }
    ]
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert started.process is server.processes[0]
    assert server.connects == [(str(path), 2.0)]
    assert (record.state, record.thread_id, record.session_id) == (CONNECTED, "thread-new", "session-new")
    assert (record.server_pid, record.server_start_identity) == (FIRST_PID, SERVER_START)
    assert (record.active_turn_id, record.last_transport_activity) == ("turn-opening", 1000)
    assert (record.execution_id, record.execution_generation, record.seat) == (agent.execution_id, 1, SEAT)
    host = socket.gethostname()
    assert (record.agent_name, record.task_id, record.host_id) == (agent.name, "task", host)
    assert (record.account, record.profile, record.bundle_revision) == ("default", "engineer", "rev-1")
    assert (record.model, record.effort, record.bridge_owner) == ("gpt-6.1-sol", "high", f"{host}:{os.getpid()}")
    admitted = store.execution(SLUG, agent.execution_id)
    assert (admitted.transport, admitted.harness, admitted.conversation_id) == (
        APP_SERVER_TRANSPORT,
        "codex",
        "thread-new",
    )
    assert admitted.runtime_target == {"pid": os.getpid(), "pid_start": OWN_START}
    assert server.params("hooks/list") == [{"cwds": [str(tmp_path)]}]
    assert server.params("thread/start") == [{"cwd": str(tmp_path)}]
    assert server.params("turn/start") == [
        {"threadId": "thread-new", "input": [{"type": "text", "text": "Start the task."}]}
    ]
    assert server.params("thread/name/set") == [{"threadId": "thread-new", "name": agent.name}]
    assert server.methods().index("thread/start") < server.methods().index("thread/name/set")
    assert server.methods().index("thread/name/set") < server.methods().index("turn/start")
    assert not BindingStore(store).claim_opening(SLUG, agent.execution_id, 2000)
    assert all(connection.closed for connection in server.connections)


def test_local_codex_app_server_admission_records_the_root_hive(store, server, clock, root, tmp_path, monkeypatch):
    from scripts.swarm import commands

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation-root")
    spec = launch_spec(store, server, tmp_path)
    admitted = supervisor.admit(store, spec, deps(server, clock, root))
    assert admitted.hive == "workstation-root"
    assert store.execution(SLUG, admitted.execution_id).hive == "workstation-root"


def test_no_opening_prompt_sends_no_turn(store, server, clock, root, tmp_path):
    started = supervisor.launch(store, launch_spec(store, server, tmp_path, opening=""), deps(server, clock, root))
    assert "turn/start" not in server.methods()
    assert started.record.active_turn_id == ""
    assert BindingStore(store).claim_opening(SLUG, started.agent.execution_id, 2000)


def test_a_thread_without_a_session_id_binds_an_empty_one(store, server, clock, root, tmp_path):
    server.thread = {"id": "thread-bare"}
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    assert (started.record.thread_id, started.record.session_id) == ("thread-bare", "")


def test_an_unreadable_own_start_admits_without_a_runtime_target(store, server, clock, root, tmp_path):
    server.starts.pop(os.getpid())
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    assert store.execution(SLUG, started.agent.execution_id).runtime_target == {}


def test_an_unreadable_server_start_records_zero_and_fails_ownership(store, server, clock, root, tmp_path):
    server.server_start = None
    failed(store, server, clock, root, launch_spec(store, server, tmp_path), "server_identity")
    assert only_record(store).server_start_identity == 0


def test_a_duplicate_launch_for_an_occupied_seat_spawns_nothing(store, server, clock, root, tmp_path):
    first = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    with pytest.raises(SwarmError, match="replacement does not match the current execution"):
        supervisor.launch(store, launch_spec(store, server, tmp_path, first.agent.name), deps(server, clock, root))
    assert len(server.commands) == 1
    assert [agent.execution_id for agent in store.executions(SLUG, SEAT)] == [first.agent.execution_id]


def test_a_stale_predecessor_is_refused_before_any_spawn(store, server, clock, root, tmp_path):
    first = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    stale = launch_spec(store, server, tmp_path, first.agent.name, previous_execution_id="exe-stale")
    with pytest.raises(SwarmError, match="replacement does not match the current execution"):
        supervisor.launch(store, stale, deps(server, clock, root))
    assert len(server.commands) == 1
    current = launch_spec(store, server, tmp_path, first.agent.name, previous_execution_id=first.agent.execution_id)
    second = supervisor.launch(store, current, deps(server, clock, root))
    assert second.agent.generation == 2
    assert second.record.endpoint_ref != first.record.endpoint_ref


def test_a_swarm_on_the_legacy_transport_admits_no_app_server(store, server, clock, root, tmp_path):
    store.update(SLUG, codex_inbox_transport="legacy")
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "transport")
    assert str(failure) == "swarm fixture runs the legacy Codex inbox transport"
    assert server.commands == []
    assert store.executions(SLUG, SEAT) == []


def test_a_resume_binds_the_existing_thread_without_starting_one(store, server, clock, root, tmp_path):
    spec = launch_spec(store, server, tmp_path, resume_thread="thread-kept")
    started = supervisor.launch(store, spec, deps(server, clock, root))
    assert (started.record.thread_id, started.record.session_id) == ("thread-kept", "session-resumed")
    assert "thread/start" not in server.methods()
    assert server.params("thread/resume") == [{"threadId": "thread-kept"}]
    assert server.params("thread/name/set") == [{"threadId": "thread-kept", "name": spec.agent.name}]


def test_a_launch_without_an_opening_still_names_its_thread(store, server, clock, root, tmp_path):
    started = supervisor.launch(store, launch_spec(store, server, tmp_path, opening=""), deps(server, clock, root))
    assert server.params("thread/name/set") == [{"threadId": "thread-new", "name": started.agent.name}]


def test_a_refused_thread_name_still_opens_the_turn(store, server, clock, root, tmp_path):
    server.refuse = {"thread/name/set"}
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    assert (started.record.state, started.record.active_turn_id) == (CONNECTED, "turn-opening")
    assert server.methods().index("thread/name/set") < server.methods().index("turn/start")


def test_a_connection_lost_while_naming_the_thread_fails_the_launch(store, server, clock, root, tmp_path):
    server.lose = {"thread/name/set"}
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path, opening=""), "protocol")
    assert str(failure) == "thread/name/set lost the connection: scripted"
    assert server.processes[-1].poll() is not None


@pytest.mark.parametrize("account", ["default", "quota-successor"])
def test_handoff_and_quota_successors_admit_a_new_server_and_resume_the_thread(
    store, server, clock, root, tmp_path, account
):
    from scripts.inbox.codex import StaleExecution, Transport

    first = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    next_spec = launch_spec(
        store,
        server,
        tmp_path,
        resume_thread=first.record.thread_id,
        previous_execution_id=first.agent.execution_id,
    )
    next_spec = replace(next_spec, agent=replace(next_spec.agent, account=account))
    successor = supervisor.launch(store, next_spec, deps(server, clock, root))
    assert successor.agent.generation == first.agent.generation + 1
    assert successor.record.server_pid != first.record.server_pid
    assert successor.record.thread_id == first.record.thread_id
    assert successor.record.account == account
    assert store.execution_occupants(SLUG)[SEAT].execution_id == successor.agent.execution_id
    assert server.params("thread/resume") == [{"threadId": first.record.thread_id}]
    with pytest.raises(StaleExecution):
        Transport(BindingStore(store), SLUG, first.agent.execution_id).require()
    supervisor.finish(store, SLUG, first, 2000)
    assert store.execution_occupants(SLUG)[SEAT].execution_id == successor.agent.execution_id


def test_finish_clears_a_dead_bridges_abandoned_publication(store, server, clock, root, tmp_path):
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    key = store.key(SLUG, "execution-publishing")
    store.redis.hset(key, SEAT, started.agent.execution_id)
    supervisor.finish(store, SLUG, started, 2000)
    assert not store.redis.hgetall(key)


@pytest.mark.parametrize("resource", ["endpoint", "publication", "transport"])
def test_terminal_cleanup_keeps_resources_that_change_during_release(
    store, server, clock, root, tmp_path, monkeypatch, resource
):
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    bindings = BindingStore(store)
    record = replace(started.record, state=EXITED)
    bindings.put(SLUG, record)
    endpoints, publishing = store.key(SLUG, ENDPOINTS), store.key(SLUG, "execution-publishing")
    store.redis.hset(publishing, SEAT, record.execution_id)
    original, changed = bindings.get, []

    def get(*args):
        observed = original(*args)
        if not changed:
            changed.append(True)
            if resource == "endpoint":
                store.redis.hset(endpoints, record.endpoint_ref, "successor")
            elif resource == "publication":
                store.redis.hset(publishing, SEAT, "successor")
            else:
                bindings.put(SLUG, replace(record, state=CONNECTED))
        return observed

    monkeypatch.setattr(bindings, "get", get)
    bindings.release(SLUG, record)
    if resource == "endpoint":
        assert store.redis.hget(endpoints, record.endpoint_ref) == "successor"
    elif resource == "publication":
        assert store.redis.hget(publishing, SEAT) == "successor"
    else:
        assert store.redis.hget(publishing, SEAT) == record.execution_id


def test_a_thread_without_an_id_fails_as_a_protocol_error(store, server, clock, root, tmp_path):
    server.thread = {"sessionId": "session-only"}
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "protocol")
    assert str(failure) == supervisor.NO_THREAD
    assert_failed_cleanly(store, server, "protocol")


def test_a_refused_protocol_call_fails_with_its_message(store, server, clock, root, tmp_path):
    server.refuse = {"thread/start"}
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "protocol")
    assert str(failure) == "thread/start refused: scripted"
    assert_failed_cleanly(store, server, "protocol")


def test_a_server_running_from_another_codex_home_is_refused(store, server, clock, root, tmp_path):
    server.info = {"codexHome": str(tmp_path / "other-profile")}
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "wrong_profile")
    assert str(failure) == supervisor.OTHER_HOME
    assert_failed_cleanly(store, server, "wrong_profile")


@pytest.mark.parametrize("info", [{}, {"codexHome": ""}] * 10)
def test_a_server_that_names_no_codex_home_is_refused_even_from_the_cwd(
    store, server, clock, root, tmp_path, monkeypatch, info
):
    server.info = info
    spec = launch_spec(store, server, tmp_path)
    assert Path(spec.codex_home).is_relative_to(tmp_path)
    check_binding = supervisor.check_binding

    def check_from_home(connection, info, launch):
        with monkeypatch.context() as scoped_patch:
            scoped_patch.chdir(server.runtime_home)
            assert Path.cwd() == Path(server.runtime_home)
            return check_binding(connection, info, launch)

    monkeypatch.setattr(supervisor, "check_binding", check_from_home)
    failed(store, server, clock, root, spec, "wrong_profile")
    assert_failed_cleanly(store, server, "wrong_profile")


def test_a_server_signed_in_with_another_account_kind_is_refused(store, server, clock, root, tmp_path):
    server.account = "apiKey"
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "wrong_account")
    assert str(failure) == "the server is not signed in with a chatgpt account"
    assert_failed_cleanly(store, server, "wrong_account")


def test_an_unchecked_account_kind_reads_no_account(store, server, clock, root, tmp_path):
    supervisor.launch(store, launch_spec(store, server, tmp_path, account_type=""), deps(server, clock, root))
    assert "account/read" not in server.methods()


def test_other_account_credentials_in_the_child_env_refuse_before_admission(store, server, clock, root, tmp_path):
    spec = launch_spec(store, server, tmp_path)
    environ = {**spec.environ, "AH_CX_TOKEN_other": "unused", "AH_CX_TOKEN_b": "unused"}
    spec = replace(spec, environ=environ, allowed_tokens=frozenset({"AH_CX_TOKEN_mine"}))
    failure = failed(store, server, clock, root, spec, "wrong_account")
    assert str(failure) == "child environment carries other account credentials: ['AH_CX_TOKEN_b', 'AH_CX_TOKEN_other']"
    assert server.commands == []
    assert store.executions(SLUG, SEAT) == []


def test_the_selected_account_credential_is_allowed(store, server, clock, root, tmp_path):
    spec = launch_spec(store, server, tmp_path)
    spec = replace(
        spec, environ={**spec.environ, "AH_CX_TOKEN_mine": "unused"}, allowed_tokens=frozenset({"AH_CX_TOKEN_mine"})
    )
    supervisor.launch(store, spec, deps(server, clock, root))
    assert server.spawns[0]["env"]["AH_CX_TOKEN_mine"] == "unused"


def test_missing_required_hooks_are_refused(store, server, clock, root, tmp_path):
    server.hooks = hooks(server.source, events=("sessionStart", "userPromptSubmit", "preToolUse", "postToolUse"))
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "hooks_missing")
    assert str(failure) == "required profile hooks are missing: ['stop']"
    assert_failed_cleanly(store, server, "hooks_missing")


def test_untrusted_hooks_are_trusted_before_the_thread_starts(store, server, clock, root, tmp_path):
    server.hooks = hooks(server.source, trust="untrusted")
    supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    methods = server.methods()
    assert methods.index("config/batchWrite") < methods.index("thread/start")
    assert server.params("hooks/list") == [{"cwds": [str(tmp_path)]}] * 2
    edits = server.params("config/batchWrite")[0]["edits"]
    assert edits[0] == {
        "keyPath": 'hooks.state."k:sessionStart".trusted_hash',
        "value": "h",
        "mergeStrategy": "replace",
    }


def test_hooks_that_stay_untrusted_are_refused(store, server, clock, root, tmp_path):
    server.hooks = hooks(server.source, trust="untrusted", events=("stop", *supervisor.REQUIRED_HOOKS[:4]))
    server.trust_to = "untrusted"
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "hooks_untrusted")
    assert str(failure) == (
        "required hooks stay untrusted: ['postToolUse', 'preToolUse', 'sessionStart', 'stop', 'userPromptSubmit']"
    )
    assert_failed_cleanly(store, server, "hooks_untrusted")


def test_a_hook_from_outside_the_profile_home_is_never_trusted_or_counted(
    store, server, clock, root, tmp_path, monkeypatch
):
    Path(server.codex_home).mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(server.codex_home)
    repo_hooks = hooks(str(tmp_path / ".codex" / "hooks.json"), trust="untrusted")
    unsourced = [{key: value for key, value in hook.items() if key != "sourcePath"} for hook in repo_hooks]
    server.hooks = hooks(server.source) + repo_hooks + unsourced
    supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    assert "config/batchWrite" not in server.methods()
    server.hooks = repo_hooks + unsourced
    failed(store, server, clock, root, other_seat(launch_spec(store, server, tmp_path)), "hooks_missing")


def test_a_server_that_came_up_but_never_answered_times_out_and_is_stopped(store, server, clock, root, tmp_path):
    server.answers = False
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "startup_timeout")
    assert str(failure) == supervisor.NEVER_ANSWERED
    record = only_record(store)
    assert (record.server_pid, record.server_start_identity) == (FIRST_PID, SERVER_START)
    assert not Path(record.endpoint_ref.removeprefix("unix://")).exists()
    assert server.processes[0].stopped
    assert clock.now == 2.0
    assert [timeout for _, timeout in server.connects] == [2.0 - 0.25 * step for step in range(8)]
    assert_failed_cleanly(store, server, "startup_timeout")


def test_a_server_whose_socket_never_appears_is_never_connected(store, server, clock, root, tmp_path):
    original = server.popen

    def no_socket(command, **kwargs):
        process = original(command, **kwargs)
        Path(command[3].removeprefix("unix://")).unlink()
        return process

    server.popen = no_socket
    failed(store, server, clock, root, launch_spec(store, server, tmp_path), "startup_timeout")
    assert server.connects == []


def test_a_server_that_exits_before_it_answers_fails_without_waiting(store, server, clock, root, tmp_path):
    server.exits = True
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "server_exited")
    assert str(failure) == "app-server exited with status 1 before it answered"
    assert clock.now == 0.0
    assert only_record(store).state == FAILED


def test_a_missing_codex_binary_fails_the_launch_and_frees_the_endpoint(store, server, clock, root, tmp_path):
    def missing(command, **kwargs):
        raise FileNotFoundError("codex")

    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "spawn", popen=missing)
    assert str(failure) == "app-server could not start: codex"
    record = only_record(store)
    assert (record.state, record.last_error_class, record.server_pid) == (FAILED, "spawn", 0)
    assert store.redis.hgetall(store.key(SLUG, ENDPOINTS)) == {}


def test_any_error_after_the_spawn_stops_the_server(store, server, clock, root, tmp_path, monkeypatch):
    def refused(slug, agent):
        raise SwarmError("agent registry kept changing; update was not committed")

    monkeypatch.setattr(store, "put_agent", refused)
    with pytest.raises(SwarmError, match="agent registry kept changing"):
        supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    assert server.processes[0].stopped
    record = only_record(store)
    assert (record.state, record.last_error_class) == (FAILED, "SwarmError")
    assert store.redis.hgetall(store.key(SLUG, ENDPOINTS)) == {}


def test_a_reused_pid_is_not_mistaken_for_the_server():
    record = supervisor.TransportRecord("exe-1", 1, SEAT, "n", "t", "unix:///s", server_pid=7, server_start_identity=5)
    assert supervisor.owns(record, {7: 5}.get)
    assert not supervisor.owns(record, {7: 6}.get)
    assert not supervisor.owns(record, {}.get)
    assert supervisor.owns(replace(record, server_pid=1, server_start_identity=1), {1: 1}.get)
    assert not supervisor.owns(replace(record, server_start_identity=0), {7: 0}.get)
    assert not supervisor.owns(replace(record, server_pid=0), {0: 5}.get)


def test_a_pid_taken_over_during_startup_fails_the_launch(store, server, clock, root, tmp_path):
    original = server.start_identity
    server.start_identity = lambda pid: SERVER_START + 1 if clock.now > 0 else original(pid)
    server.answers = False
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "server_identity")
    assert str(failure) == supervisor.PID_TAKEN


def test_two_executions_get_two_private_endpoints(store, server, clock, root, tmp_path):
    first = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    second = supervisor.launch(store, other_seat(launch_spec(store, server, tmp_path)), deps(server, clock, root))
    assert first.record.endpoint_ref != second.record.endpoint_ref
    assert len(store.redis.hgetall(store.key(SLUG, ENDPOINTS))) == 2


def test_concurrent_executions_write_private_seeded_homes(store, server, root, tmp_path):
    profile = Path(server.codex_home)
    (profile / "config.toml").write_text('model = "fixture"\n')
    (profile / "hooks.json").write_text("{}")
    shared = tmp_path / "shared-skills"
    shared.mkdir()
    (shared / "SKILL.md").write_text("fixture skill")
    (profile / "skills").symlink_to(shared, target_is_directory=True)
    (profile / "history.jsonl").symlink_to(tmp_path / "missing-history")
    original = {p.name: p.read_bytes() for p in profile.iterdir() if p.is_file()}
    barrier = Barrier(2)
    servers = [Server(server.codex_home), Server(server.codex_home)]
    specs = [launch_spec(store, item, tmp_path) for item in servers]
    specs[1] = other_seat(specs[1])

    def start(index):
        item = servers[index]

        def popen(command, **kwargs):
            home = Path(kwargs["env"]["CODEX_HOME"])
            barrier.wait(timeout=10)
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os; from pathlib import Path; h = Path(os.environ['CODEX_HOME']); "
                    "assert (h / 'skills' / 'SKILL.md').read_text() == 'fixture skill'; "
                    "(h / 'config.toml').write_text(os.environ['EXECUTION_WRITER']); "
                    "(h / 'hooks.json').write_text(os.environ['EXECUTION_WRITER']); "
                    "(h / 'history.jsonl').write_text(os.environ['EXECUTION_WRITER']); "
                    "(h / 'skills' / 'SKILL.md').write_text(os.environ['EXECUTION_WRITER'])",
                ],
                env={**kwargs["env"], "EXECUTION_WRITER": str(index)},
                check=True,
            )
            item.info = {"codexHome": str(home)}
            item.hooks = hooks(str(home / "hooks.json"))
            return item.popen(command, **kwargs)

        return supervisor.launch(store, specs[index], deps(item, Clock(), root, popen=popen))

    with ThreadPoolExecutor(max_workers=2) as pool:
        started = list(pool.map(start, range(2)))
    homes = [Path(item.spawns[0]["env"]["CODEX_HOME"]) for item in servers]
    assert homes[0] != homes[1]
    assert profile not in homes
    assert len({item.record.execution_id for item in started}) == 2
    for index, home in enumerate(homes):
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        assert all((home / name).read_text() == str(index) for name in ("config.toml", "hooks.json", "history.jsonl"))
        assert (home / "skills" / "SKILL.md").read_text() == str(index)
        assert not (home / "skills").is_symlink()
        assert specs[index].environ["CODEX_HOME"] == str(profile)
    assert {p.name: p.read_bytes() for p in profile.iterdir() if p.is_file()} == original
    assert (shared / "SKILL.md").read_text() == "fixture skill"
    assert (profile / "skills").is_symlink()
    assert (profile / "history.jsonl").is_symlink()
    assert not (tmp_path / "missing-history").exists()


def test_execution_home_keeps_profile_binding_and_manifests(server, tmp_path):
    import json

    from scripts.profiles import binding

    profile = Path(server.codex_home)
    (profile / "AGENTS.md").write_text("fixture persona")
    manifest = profile.parent / "codex.sources.json"
    manifest.write_text("[]")
    mounts = profile.parent / "codex.mounts.json"
    mounts.write_text('{"fixture": "mount"}')
    binding.write(profile, "engineer", "codex")
    original = (profile / binding.FILE).read_bytes()
    directory = tmp_path / "execution"
    directory.mkdir()
    spec = supervisor.Launch("fixture", AgentRecord("fixture", "eng", "task"), str(tmp_path), str(profile), {})
    private = supervisor.execution_home(spec, directory)
    home = Path(private.codex_home)
    assert binding.inspect(home, "engineer", "codex")["home"] == str(home)
    assert (directory / "codex.sources.json").read_bytes() == manifest.read_bytes()
    assert (directory / "codex.mounts.json").read_bytes() == mounts.read_bytes()
    assert (profile / binding.FILE).read_bytes() == original
    assert json.loads(original)["home"] == str(profile)


@pytest.mark.parametrize("reported", [True, False])
def test_execution_home_rerecords_the_profile_report_for_the_copy(server, tmp_path, reported):
    import json

    from scripts.profiles import binding

    profile = Path(server.codex_home)
    (profile / "AGENTS.md").write_text(binding.persona("Engineer instructions.\n"))
    (profile.parent / "codex.sources.json").write_text("[]")
    binding.write(profile, "engineer", "codex")
    report = tmp_path / "report.json"
    binding.request(report, "engineer", "codex", profile)
    launched = report.read_bytes()
    directory = tmp_path / "execution"
    directory.mkdir()
    environ = {binding.REPORT: str(report)} if reported else {}
    spec = supervisor.Launch("fixture", AgentRecord("fixture", "eng", "task"), str(tmp_path), str(profile), environ)

    home = Path(supervisor.execution_home(spec, directory).codex_home)

    if not reported:
        assert report.read_bytes() == launched
        return
    rendered = json.loads(report.read_text())["rendered"]
    assert rendered["home"] == str(home.resolve())
    assert rendered["canary"] == json.loads(launched)["rendered"]["canary"]


@pytest.mark.parametrize("linked", [False, True])
def test_execution_home_shares_package_binaries_without_copying(server, tmp_path, linked):
    profile = Path(server.codex_home)
    package = tmp_path / "package" if linked else profile / "bin"
    package.mkdir()
    binary = package / "codex"
    binary.write_bytes(b"fixture package binary")
    skill_bin = profile / "skills" / "tool" / "bin"
    skill_bin.mkdir(parents=True)
    (skill_bin / "tool").write_text("private skill tool")
    if linked:
        (profile / "bin").symlink_to(package, target_is_directory=True)
    directory = tmp_path / "execution"
    directory.mkdir()
    spec = supervisor.Launch("fixture", AgentRecord("fixture", "eng", "task"), str(tmp_path), str(profile), {})

    private = supervisor.execution_home(spec, directory)

    home = Path(private.codex_home)
    assert (home / "bin").is_symlink()
    assert (home / "bin").resolve() == package.resolve()
    assert (home / "bin" / "codex").samefile(binary)
    assert binary.read_bytes() == b"fixture package binary"
    private_tool = home / "skills" / "tool" / "bin" / "tool"
    assert private_tool.read_text() == "private skill tool"
    assert not private_tool.samefile(skill_bin / "tool")


def test_a_home_seed_failure_spawns_nothing_and_frees_the_endpoint(store, server, clock, root, tmp_path):
    spec = launch_spec(store, server, tmp_path, codex_home=str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError):
        supervisor.launch(store, spec, deps(server, clock, root))
    assert not server.spawns
    record = only_record(store)
    assert record.state == FAILED
    assert record.last_error_class == "FileNotFoundError"
    assert not store.redis.hgetall(store.key(SLUG, ENDPOINTS))


@pytest.mark.parametrize("stage", ["seed", "spawn", "startup", "registry"])
def test_failed_launch_removes_only_its_execution_folder(store, server, clock, root, tmp_path, monkeypatch, stage):
    neighbor = root / "another-execution"
    neighbor.mkdir(parents=True)
    (neighbor / "keep").write_text("other session")
    profile = Path(server.codex_home)
    (profile / "config.toml").write_text("private seed")
    package = tmp_path / "package"
    package.mkdir()
    (package / "codex").write_text("shared binary")
    (profile / "bin").symlink_to(package, target_is_directory=True)
    spec = launch_spec(store, server, tmp_path)
    built = deps(server, clock, root)
    if stage == "seed":
        copytree = shutil.copytree

        def partial_seed(source, destination, **kwargs):
            copytree(source, destination, **kwargs)
            raise OSError("home seed failed")

        monkeypatch.setattr(supervisor.shutil, "copytree", partial_seed)
    elif stage == "spawn":

        def refused_spawn(*args, **kwargs):
            raise OSError("spawn failed")

        built = replace(built, popen=refused_spawn)
    elif stage == "startup":
        server.answers = False
    else:

        def refused_registry(*args):
            raise SwarmError("registry failed")

        monkeypatch.setattr(store, "put_agent", refused_registry)

    with pytest.raises((OSError, supervisor.LaunchFailure, SwarmError)):
        supervisor.launch(store, spec, built)

    assert list(root.iterdir()) == [neighbor]
    assert (neighbor / "keep").read_text() == "other session"
    assert (profile / "config.toml").read_text() == "private seed"
    assert (package / "codex").read_text() == "shared binary"
    assert not store.redis.hgetall(store.key(SLUG, ENDPOINTS))
    assert all(process.poll() is not None for process in server.processes)


def test_failed_execution_directory_permissions_leave_no_folder(store, server, clock, root, tmp_path, monkeypatch):
    chmod = Path.chmod

    def refused_permissions(path, mode):
        if path.parent == root:
            raise OSError("directory permissions failed")
        chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", refused_permissions)
    with pytest.raises(OSError, match="directory permissions failed"):
        supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    assert list(root.iterdir()) == []
    assert not server.spawns


def test_failed_process_stop_still_removes_execution_folder(store, server, clock, root, tmp_path, monkeypatch):
    server.answers = False
    popen = server.popen

    def spawn(command, **kwargs):
        process = popen(command, **kwargs)

        def refused_wait(timeout=None):
            raise OSError("process wait failed")

        monkeypatch.setattr(process, "wait", refused_wait)
        return process

    with pytest.raises(OSError, match="process wait failed"):
        supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root, popen=spawn))
    assert server.processes[0].poll() is not None
    assert list(root.iterdir()) == []
    assert not store.redis.hgetall(store.key(SLUG, ENDPOINTS))


@pytest.mark.parametrize("stage", ["endpoint", "reservation"])
def test_failure_before_transport_reservation_ends_admitted_execution(
    store, server, clock, root, tmp_path, monkeypatch, stage
):
    spec = launch_spec(store, server, tmp_path)
    failure = RuntimeError("reservation unavailable")

    def refuse(*args):
        raise failure

    monkeypatch.setattr(supervisor, "socket_path" if stage == "endpoint" else "_record", refuse)
    with pytest.raises(RuntimeError) as caught:
        supervisor.launch(store, spec, deps(server, clock, root))
    assert caught.value is failure
    assert not server.spawns
    assert not store.agents(SLUG)
    assert store.execution_occupants(SLUG)[SEAT].state == "finished"
    import json

    assert json.loads(store.redis.lrange(store.key(SLUG, "history"), 0, -1)[0])["ended_at"] == 1000
    assert not store.redis.hgetall(store.key(SLUG, ENDPOINTS))


def test_the_runtime_root_comes_from_the_launch_environment_when_deps_name_none(store, server, clock, tmp_path):
    runtime = Path(tempfile.mkdtemp(prefix="rt-"))
    try:
        spec = launch_spec(store, server, tmp_path)
        spec = replace(spec, environ={**spec.environ, "HOME": str(runtime), "XDG_RUNTIME_DIR": "/run/user/7"})
        started = supervisor.launch(store, spec, deps(server, clock, None))
        assert started.record.endpoint_ref.startswith(f"unix://{runtime}/.agentihooks/cx/")
    finally:
        shutil.rmtree(runtime)


def test_the_server_command_never_carries_no_daemon(root):
    with pytest.raises(supervisor.LaunchFailure) as failure:
        supervisor.server_command("codex", root / "s", ("-c", "x=1", "--no-daemon"))
    assert (failure.value.error_class, str(failure.value)) == ("command", supervisor.DAEMON_REFUSED)
    assert supervisor.server_command("codex", root / "s", ("-c", "x=1")) == [
        "codex",
        "app-server",
        "--listen",
        f"unix://{root / 's'}",
        "-c",
        "x=1",
    ]


def test_the_socket_path_is_short_private_and_nested(root):
    nested = root / "a" / "b"
    path = supervisor.socket_path(nested, "exe-0123456789abcdef0123")
    assert path == nested / "0123456789abcdef" / "s"
    assert stat.S_IMODE(nested.stat().st_mode) == 0o700
    assert supervisor.socket_path(nested, "exe-0123456789abcdef0123") == path


def test_a_socket_path_past_the_limit_is_refused_and_one_at_it_is_kept(tmp_path):
    base = Path("/") / ("d" * (supervisor.SOCKET_PATH_MAX - len("/0123456789abcdef/s") - 1))
    assert len(str(base / "0123456789abcdef" / "s")) == supervisor.SOCKET_PATH_MAX
    with pytest.raises(supervisor.LaunchFailure) as failure:
        supervisor.socket_path(base / "x", "exe-0123456789abcdef")
    assert (failure.value.error_class, str(failure.value)) == ("endpoint", supervisor.LONG_SOCKET)
    short = Path(tempfile.mkdtemp(prefix="cx-"))
    try:
        padded = short / ("d" * (supervisor.SOCKET_PATH_MAX - len(f"{short}/") - len("/0123456789abcdef/s")))
        assert len(str(supervisor.socket_path(padded, "exe-0123456789abcdef"))) == supervisor.SOCKET_PATH_MAX
    finally:
        shutil.rmtree(short)


def test_the_runtime_root_ignores_the_memory_backed_user_runtime_dir(monkeypatch):
    assert supervisor.runtime_root({"XDG_RUNTIME_DIR": "/run/user/7", "HOME": "/home/u"}) == Path(
        "/home/u/.agentihooks/cx"
    )
    assert supervisor.runtime_root({"HOME": "/home/u"}) == Path("/home/u/.agentihooks/cx")
    monkeypatch.setattr(supervisor.Path, "home", classmethod(lambda cls: Path("/home/fallback")))
    assert supervisor.runtime_root({}) == Path("/home/fallback/.agentihooks/cx")


def test_stop_kills_a_server_that_ignores_terminate():
    process = Process(9, ignores_terminate=True)
    supervisor.stop(process)
    assert (process.stopped, process.killed, process.waits) == (True, True, [supervisor.STOP_WAIT_S, None])
    exited = Process(10)
    exited.returncode = 0
    supervisor.stop(exited)
    assert (exited.stopped, exited.waits) == (False, [])


def test_time_ms_reads_the_wall_clock_in_milliseconds(monkeypatch):
    monkeypatch.setattr(supervisor.time, "time", lambda: 12.3456)
    assert supervisor.time_ms() == 12345


def test_finish_marks_the_stored_record_exited_and_frees_the_endpoint(store, server, clock, root, tmp_path):
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    BindingStore(store).put(SLUG, replace(started.record, last_reconcile_at=42))
    record = supervisor.finish(store, SLUG, started, 5000)
    assert (record.state, record.active_turn_id, record.last_transport_activity) == (EXITED, "", 5000)
    assert record.last_reconcile_at == 42
    assert BindingStore(store).get(SLUG, started.record.execution_id) == record
    assert store.redis.hgetall(store.key(SLUG, ENDPOINTS)) == {}


def test_the_protocol_module_is_the_default_connector():
    assert supervisor.Deps().connect is protocol.connect


CAPACITY = "Selected model is at capacity. Please try a different model."


def item(kind):
    return {"method": "item/started", "params": {"item": {"type": kind, "id": kind}}}


def completed(status, message=""):
    turn = {"id": "turn-opening", "status": status, "error": {"message": message} if message else None}
    return {"method": "turn/completed", "params": {"turn": turn}}


def reported_spec(store, server, tmp_path):
    from scripts.profiles import binding

    profile = Path(server.codex_home)
    (profile / "AGENTS.md").write_text(binding.persona("Engineer instructions.\n"))
    (profile.parent / "codex.sources.json").write_text("[]")
    binding.write(profile, "engineer", "codex")
    report = tmp_path / "report.json"
    binding.request(report, "engineer", "codex", profile)
    spec = launch_spec(store, server, tmp_path)
    return replace(spec, environ={**spec.environ, binding.REPORT: str(report)}), report


def test_an_opening_turn_ended_on_capacity_is_started_once_more(store, server, clock, root, tmp_path):
    started_turn = {"method": "turn/started", "params": {"turn": {"id": "turn-opening", "status": "inProgress"}}}
    opening = [started_turn, item("userMessage"), item("hookPrompt"), completed("failed", CAPACITY)]
    server.turn_events = [opening, [item("reasoning")]]
    spec, report = reported_spec(store, server, tmp_path)
    clock.now = 100.0

    started = supervisor.launch(store, spec, deps(server, clock, root))

    opening = {"threadId": "thread-new", "input": [{"type": "text", "text": "Start the task."}]}
    assert server.params("turn/start") == [opening, opening]
    assert (started.record.state, started.record.active_turn_id) == (CONNECTED, "turn-opening")
    assert json.loads(report.read_text())["state"] == "pending"
    assert set(server.polls) == {supervisor.OPENING_WATCH_S}


def test_a_second_capacity_error_fails_the_launch_naming_it(store, server, clock, root, tmp_path):
    server.turn_events = [[completed("failed", CAPACITY)], [completed("failed", CAPACITY)]]
    spec, report = reported_spec(store, server, tmp_path)

    failure = failed(store, server, clock, root, spec, "capacity")

    assert len(server.params("turn/start")) == 2
    reason = f"the opening Codex turn ended twice on a provider capacity error: {CAPACITY}"
    assert str(failure) == reason
    assert json.loads(report.read_text())["state"] == "failed"
    assert json.loads(report.read_text())["reason"] == reason
    record = only_record(store)
    assert (record.state, record.last_error_class) == (FAILED, "capacity")
    assert server.processes[-1].poll() is not None
    from scripts.profiles import binding

    with pytest.raises(ValueError, match="ended twice on a provider capacity error: Selected model is at capacity"):
        binding.wait(report, 1)


@pytest.mark.parametrize(
    "events",
    [
        [completed("failed", "stream disconnected before completion")],
        [completed("completed")],
        [completed("failed")],
        [item("agentMessage"), completed("failed", CAPACITY)],
    ],
)
def test_an_opening_turn_without_a_capacity_failure_is_not_started_again(store, server, clock, root, tmp_path, events):
    server.turn_events = [events, [completed("failed", CAPACITY)]]

    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))

    assert len(server.params("turn/start")) == 1
    assert started.record.state == CONNECTED


def test_a_capacity_retry_without_a_profile_report_still_fails_the_launch(store, server, clock, root, tmp_path):
    server.turn_events = [[completed("failed", CAPACITY)], [completed("failed", CAPACITY)]]

    failed(store, server, clock, root, launch_spec(store, server, tmp_path), "capacity")

    assert len(server.params("turn/start")) == 2


def test_the_opening_watch_ends_at_its_deadline(store, server, clock, root, tmp_path, monkeypatch):
    server.turn_events = [[completed("failed", CAPACITY)]]
    monkeypatch.setattr(supervisor, "OPENING_WATCH_S", 0.0)

    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))

    assert len(server.params("turn/start")) == 1
    assert started.record.state == CONNECTED
