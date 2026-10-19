"""Tests for hooks.context.account_sessions — live sessions per account from /proc."""

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.context import account_sessions as acc


def _proc(root, pid, comm, ppid, argv, env):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "comm").write_text(comm + "\n")
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0\n")
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")


def _tree(tmp_path):
    root = tmp_path / "proc"
    _proc(
        root,
        100,
        "claude",
        1,
        ["/home/u/.local/bin/claude", "--dangerously-skip-permissions"],
        {"AH_CC_TOKEN_alpha": "secret-a", "CLAUDE_CODE_OAUTH_TOKEN": "secret-a"},
    )
    _proc(root, 101, "claude", 1, ["claude", "--resume", "x"], {"AH_CC_TOKEN_alpha": "secret-a"})
    _proc(root, 102, "claude", 1, ["claude"], {"AH_CC_TOKEN_beta": "secret-b"})
    _proc(root, 103, "claude", 1, ["claude"], {"HOME": "/home/u"})
    _proc(root, 104, "claude", 100, ["claude", "-p", "Reply with exactly OK."], {})
    _proc(root, 105, "bash", 100, ["bash", "-c", "cd x && python -m hooks"], {"AH_CC_TOKEN_alpha": "secret-a"})
    _proc(root, 106, "python3", 105, ["python3", "-m", "hooks"], {"AH_CC_TOKEN_alpha": "secret-a"})
    _proc(
        root,
        107,
        "node",
        1,
        ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"],
        {"AH_CC_TOKEN_beta": "secret-b"},
    )
    return root


def test_account_comes_from_the_single_token_name():
    assert acc.account_from_names(["HOME", "AH_CC_TOKEN_alpha"]) == "alpha"
    assert acc.account_from_names(["AH_CC_TOKEN_alpha", "AH_CC_TOKEN_beta"]) == acc.UNROUTED
    assert acc.account_from_names(["HOME"]) == acc.UNROUTED
    assert acc.environment_account({"AH_CC_TOKEN_alpha": ""}) == acc.UNROUTED


def test_an_email_named_token_counts_under_its_email_slug():
    from scripts.account_names import token_env

    assert acc.account_from_names(["HOME", token_env("NESTOR.COLT@GMAIL.COM")]) == "nestor_coltatgmail_com"


def _renamed(tmp_path):
    root = tmp_path / "proc"
    _proc(root, 300, "claude", 1, ["claude"], {"AH_CC_TOKEN_old": "secret-a"})
    _proc(root, 301, "claude", 1, ["claude"], {"AH_CC_TOKEN_old": "secret-a"})
    _proc(root, 302, "claude", 1, ["claude"], {"AH_CC_TOKEN_mail": "secret-a"})
    _proc(root, 303, "claude", 1, ["claude"], {"AH_CC_TOKEN_beta": "secret-b"})
    _proc(root, 304, "claude", 1, ["claude"], {"AH_CC_TOKEN_gone": "secret-c"})
    return root


def test_sessions_under_an_old_name_count_against_the_account_holding_the_same_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("scripts.operator_env.block_tokens", lambda: {})
    environ = {"AH_CC_TOKEN_mail": "secret-a", "AH_CC_TOKEN_beta": "secret-b", "AH_CC_TOKEN_x": ""}
    with patch.object(acc, "handed_off_pids", return_value=set()):
        counts = acc.sessions_by_account(_renamed(tmp_path), local=True, environ=environ)
    sessions = acc.live_sessions(_renamed(tmp_path / "again"), environ=environ)

    assert counts == {"mail": 3, "beta": 1, "gone": 1}
    assert sessions == {300: "mail", 301: "mail", 302: "mail", 303: "beta", 304: "gone"}
    shown = repr(counts) + repr(sessions) + "".join(capsys.readouterr())
    for secret in ("secret-a", "secret-b", "secret-c"):
        digest = hashlib.sha256(secret.encode()).hexdigest()
        assert secret not in shown and digest[:8] not in shown


def test_the_managed_block_name_wins_when_two_configured_names_hold_one_token(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.operator_env.block_tokens", lambda: {"AH_CC_TOKEN_zmail": "secret-a"})
    root = tmp_path / "proc"
    _proc(root, 400, "claude", 1, ["claude"], {"AH_CC_TOKEN_alpha": "secret-a"})
    found = acc.token_accounts({"AH_CC_TOKEN_alpha": "secret-a", "AH_CC_TOKEN_api": "secret-z", "AH_CC_TOKEN_": "s"})

    assert acc.live_sessions(root, environ={"AH_CC_TOKEN_alpha": "secret-a"}) == {400: "zmail"}
    assert list(found.values()) == ["zmail"]
    assert "secret-a" not in found


def test_an_unrouted_or_api_session_keeps_its_account(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.operator_env.block_tokens", lambda: {})
    root = tmp_path / "proc"
    _proc(root, 500, "claude", 1, ["claude"], {acc.API_MARKER: "1", "AH_CC_TOKEN_old": "secret-a"})
    _proc(root, 501, "claude", 1, ["claude"], {"AH_CC_TOKEN_old": "secret-a", "AH_CC_TOKEN_two": "secret-b"})
    _proc(root, 502, "claude", 1, ["claude"], {"HOME": "/home/u"})

    assert acc.live_sessions(root, environ={"AH_CC_TOKEN_mail": "secret-a"}) == {
        500: "api",
        501: acc.UNROUTED,
        502: acc.UNROUTED,
    }


def test_the_calling_session_resolves_its_old_name_to_the_routed_account(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.operator_env.block_tokens", lambda: {})
    monkeypatch.setenv("AH_CC_TOKEN_mail", "secret-a")
    root = tmp_path / "proc"
    _proc(root, 600, "claude", 1, ["claude"], {"AH_CC_TOKEN_old": "secret-a"})
    _proc(root, 601, "claude", 1, ["claude"], {"AH_CC_TOKEN_old": "secret-x"})

    assert acc.session_account(600, root) == "mail"
    assert acc.session_account(601, root) == "old"


def test_a_token_fingerprint_reads_raw_environment_bytes(tmp_path):
    root = tmp_path / "proc"
    (root / "700").mkdir(parents=True)
    (root / "700" / "environ").write_bytes(
        b"AH_CC_TOKEN_old=secret-\xff\0AH_CC_TOKEN_none=\0AH_CC_TOKEN_eq=a=b\0HOME=/home/u\0"
    )
    expected = hashlib.sha256(b"secret-\xff").hexdigest()

    assert acc._env_fingerprint(700, root, "AH_CC_TOKEN_eq") == hashlib.sha256(b"a=b").hexdigest()

    assert acc.token_fingerprint("secret-\udcff") == expected
    assert acc._env_fingerprint(700, root, "AH_CC_TOKEN_old") == expected
    assert acc._env_fingerprint(700, root, "AH_CC_TOKEN_none") == ""
    assert acc._env_fingerprint(700, root, "AH_CC_TOKEN_absent") == ""
    assert acc._env_fingerprint(701, root, "AH_CC_TOKEN_old") == ""


def test_the_route_marker_attributes_an_api_session():
    assert acc.account_from_names(iter(["HOME", acc.API_MARKER])) == acc.API_ACCOUNT == "api"
    assert acc.account_from_names([acc.API_MARKER, "AH_CC_TOKEN_alpha"]) == "api"
    assert acc.environment_account({acc.API_MARKER: "1", "HOME": "/home/u"}) == "api"
    assert acc.environment_account({acc.API_MARKER: "", "AH_CC_TOKEN_alpha": "a"}) == "alpha"
    assert acc.codex_account_from_names([acc.API_MARKER]) == "api"
    assert acc.codex_account_from_names([acc.API_MARKER, "AH_CX_TOKEN_one"]) == "api"


def test_live_api_sessions_count_under_the_api_account(tmp_path):
    root = tmp_path / "proc"
    _proc(root, 200, "claude", 1, ["claude"], {acc.API_MARKER: "1", "HOME": "/home/u"})

    assert acc.live_sessions(root) == {200: "api"}


def test_agent_pid_skips_the_hook_shell(tmp_path):
    root = _tree(tmp_path)

    assert acc.agent_pid(105, root) == 100
    assert acc.agent_pid(106, root) == 106
    assert acc.agent_pid(999, root) == 999


def test_live_sessions_count_interactive_claude_only(tmp_path):
    root = _tree(tmp_path)

    sessions = acc.live_sessions(root)

    assert sessions == {100: "alpha", 101: "alpha", 102: "beta", 103: acc.UNROUTED, 107: "beta"}
    assert "secret" not in repr(sessions)


def test_a_process_that_exits_mid_scan_is_skipped(tmp_path):
    root = _tree(tmp_path)
    (root / "108").mkdir()

    assert 108 not in acc.live_sessions(root)
    assert acc._cmdline(108, root) == []


def test_handed_off_sessions_free_their_slot(tmp_path):
    root = _tree(tmp_path)

    with patch.object(acc, "handed_off_pids", return_value={101}):
        counts = acc.sessions_by_account(root)

    assert counts == {"alpha": 1, "beta": 2, acc.UNROUTED: 1}


def test_fleet_slots_add_to_the_local_counts(tmp_path):
    root = _tree(tmp_path)

    with (
        patch.object(acc, "handed_off_pids", return_value=set()),
        patch.object(acc, "fleet_held", return_value={"alpha": 2, "gamma": 1}),
    ):
        counts = acc.sessions_by_account(root)

    assert counts == {"alpha": 4, "beta": 2, "gamma": 1, acc.UNROUTED: 1}


def test_fleet_slots_stay_out_while_distributed_launches_are_off():
    with patch("hooks._redis.get_redis", side_effect=AssertionError("Redis read while local")):
        assert acc.fleet_held({}) == {}
        assert acc.fleet_held({"AGENTIHOOKS_RUNTIME_BACKEND": "local"}) == {}
        assert (
            acc.fleet_held({"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes", "AGENTIHOOKS_RUNTIME_DISABLED": "kubernetes"})
            == {}
        )


def test_fleet_slots_need_redis():
    with patch("hooks._redis.get_redis", return_value=None):
        assert acc.fleet_held({"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}) == {}


def test_fleet_slots_fail_open_when_redis_breaks_mid_read():
    import fakeredis
    from redis.exceptions import ConnectionError as RedisConnectionError

    redis = fakeredis.FakeRedis(decode_responses=True)
    with (
        patch("hooks._redis.get_redis", return_value=redis),
        patch.object(redis, "scan_iter", side_effect=RedisConnectionError("down")),
    ):
        assert acc.fleet_held({"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}) == {}


def test_fleet_slots_are_read_from_the_account_store():
    import fakeredis

    from scripts.swarm.keyspace import ROOT
    from scripts.swarm_v2.accounts import RESERVED, Slot, encode

    redis = fakeredis.FakeRedis(decode_responses=True)
    slot = Slot("alpha", "fleet/eng-1@fleet", "exe-1", 1, RESERVED, 10**15)
    redis.hset(f"{ROOT}:accounts:alpha", slot.holder, encode(slot))

    with patch("hooks._redis.get_redis", return_value=redis):
        assert acc.fleet_held({"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}) == {"alpha": 1}


def test_session_account_reads_the_agent_process(tmp_path):
    root = _tree(tmp_path)

    assert acc.session_account(102, root) == "beta"


def test_live_codex_sessions_count_interactive_codex_and_exec_runs(tmp_path):
    root = tmp_path / "proc"
    _proc(root, 200, "codex", 1, ["/home/u/.local/bin/codex"], {})
    _proc(root, 201, "codex", 1, ["/home/u/.codex/packages/app-server-daemon/releases/1/bin/codex"], {})
    _proc(root, 202, "codex-code-mode", 200, ["codex-code-mode"], {})
    _proc(root, 203, "codex", 1, ["codex", "exec", "summarize"], {})
    _proc(root, 204, "codex", 1, ["codex", "--yolo"], {})
    assert acc.live_codex_sessions(root) == 3


def test_codex_sessions_count_per_codex_account(tmp_path):
    root = tmp_path / "proc"
    _proc(root, 300, "codex", 1, ["codex", "--no-daemon"], {"AH_CX_TOKEN_alpha": "cx-a", "CODEX_ACCESS_TOKEN": "cx-a"})
    _proc(root, 301, "codex", 1, ["codex", "--no-daemon"], {"AH_CX_TOKEN_alpha": "cx-a"})
    _proc(root, 302, "codex", 1, ["codex"], {"AH_CC_TOKEN_ncgma": "cc"})
    _proc(root, 303, "codex", 1, ["codex"], {"AH_CX_TOKEN_alpha": "cx-a", "AH_CX_TOKEN_beta": "cx-b"})
    _proc(root, 304, "codex", 1, ["codex", "exec", "x"], {"AH_CX_TOKEN_beta": "cx-b"})
    _proc(root, 305, "claude", 1, ["claude"], {"AH_CC_TOKEN_alpha": "cc"})
    assert acc.codex_sessions_by_account(root) == {"alpha": 2, "default": 2, "beta": 1}
    assert acc.session_account(300, root) == "alpha"
    assert acc.session_account(302, root) == "default"
    assert acc.session_account(305, root) == "alpha"


def test_a_codex_child_of_a_codex_process_is_part_of_its_session_and_a_shell_run_is_not(tmp_path):
    root = tmp_path / "proc"
    _proc(root, 700, "codex", 1, ["codex", "--no-daemon"], {"AH_CX_TOKEN_alpha": "a"})
    _proc(root, 701, "bash", 700, ["bash", "-lc", "codex exec ok"], {"AH_CX_TOKEN_alpha": "a"})
    _proc(root, 702, "codex", 701, ["codex", "exec", "ok"], {"AH_CX_TOKEN_alpha": "a"})
    _proc(root, 703, "codex", 700, ["codex", "exec", "ok"], {"AH_CX_TOKEN_alpha": "a"})
    _proc(root, 710, "python3", 1, ["python3", "-m", "hooks"], {})
    _proc(root, 711, "codex", 710, ["codex", "--no-daemon", "exec", "ok"], {"AH_CX_TOKEN_beta": "b"})
    assert acc.codex_sessions_by_account(root) == {"alpha": 2, "beta": 1}
    assert acc.live_codex_sessions(root) == 3


def test_an_exec_run_whose_prompt_names_a_server_still_counts(tmp_path):
    root = tmp_path / "proc"
    _proc(root, 720, "codex", 1, ["codex", "exec", "mcp-server"], {"AH_CX_TOKEN_alpha": "a"})
    assert acc.codex_sessions_by_account(root) == {"alpha": 1}


def _endpoint(execution_id):
    from scripts.codex_session import supervisor

    key = execution_id.removeprefix(supervisor.EXECUTION_PREFIX)[: supervisor.ENDPOINT_ID_CHARS]
    return f"{supervisor.UNIX}{Path('/run/user/1000') / supervisor.RUNTIME_SUBDIR / key / supervisor.SOCKET_NAME}"


def _managed_host(root, viewers):
    from scripts.codex_session import supervisor

    endpoint = _endpoint("exe-0123456789abcdef0123")
    socket = Path(endpoint.removeprefix(supervisor.UNIX))
    _proc(root, 800, "python3", 1, ["python3", "-m", "scripts.codex_session"], {"AH_CX_TOKEN_alpha": "a"})
    _proc(root, 801, "codex", 800, supervisor.server_command("codex", socket, ()), {"AH_CX_TOKEN_alpha": "a"})
    _proc(root, 802, "codex", 800, supervisor.server_command("codex", socket, ()), {"AH_CX_TOKEN_alpha": "a"})
    _proc(root, 803, "codex", 1, ["codex", "app-server", "--listen"], {"AH_CX_TOKEN_alpha": "a"})
    remotes = (["codex", "resume", "thread-1", "--remote", endpoint], ["codex", f"--remote={endpoint}"])
    for pid, argv in zip((810, 811), remotes[:viewers], strict=False):
        _proc(root, pid, "codex", 1, argv, {"AH_CX_TOKEN_alpha": "a"})
    _proc(root, 820, "python3", 1, ["python3", "-m", "scripts.inbox.codex", "eng-1@sw"], {"AH_CX_TOKEN_alpha": "a"})
    _proc(root, 821, "python3", 1, ["python3", "-m", "scripts.codex_app_server_probe", str(socket), "r", "s", "o"], {})
    _proc(
        root, 822, "codex", 1, ["codex", "app-server", "--listen", "unix:///tmp/probe.sock"], {"AH_CX_TOKEN_alpha": "a"}
    )
    return endpoint


@pytest.mark.parametrize("viewers", [0, 1, 2])
def test_a_managed_execution_counts_once_whatever_its_viewers(tmp_path, viewers, monkeypatch):
    root = tmp_path / "proc"
    _managed_host(root, viewers)
    monkeypatch.setattr(acc, "handed_off_pids", set)
    assert acc.codex_sessions_by_account(root, local=True) == {"alpha": 1}
    assert acc.live_codex_sessions(root) == 1
    assert acc.local_processes(root)["codex"] == {801: "alpha"}


def test_an_execution_key_names_the_execution_of_its_endpoint():
    assert acc.execution_key(_endpoint("exe-0123456789abcdef0123")) == "0123456789abcdef"
    assert acc.execution_key("unix:///tmp/probe.sock") == ""
    assert acc.execution_key("unix:///run/cx/abc/other") == ""
    assert acc.execution_key("/run/user/1000/cx/abc/s") == ""
    assert acc.execution_key("unix:///run/user/1000/cx/abc/s") == "abc"


def _held_store(monkeypatch, *rows):
    from dataclasses import replace

    import fakeredis

    from scripts.codex_session.binding import BindingStore, TransportRecord
    from scripts.swarm.store import PREFIX, RedisStore

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("hooks._redis.get_redis", lambda: redis)
    redis.sadd(f"{PREFIX}:index", "sw")
    bindings = BindingStore(RedisStore(redis))
    for execution, state, account in rows:
        record = TransportRecord(execution, 1, "eng-1@sw", "engineer@1", "t1", _endpoint(execution), account=account)
        bindings.reserve("sw", record)
        if state != "starting":
            bindings.put("sw", replace(record, state=state))
    return redis


def test_a_reserved_execution_holds_its_slot_before_its_server_is_seen(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    _held_store(monkeypatch, ("exe-a000000000000001", "starting", "alpha"))
    assert acc.codex_sessions_by_account(root) == {"alpha": 1}
    assert acc.codex_sessions_by_account(root, local=True) == {}


def test_a_held_execution_and_its_observed_server_count_once(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    _managed_host(root, 2)
    _held_store(monkeypatch, ("exe-0123456789abcdef0123", "connected", "alpha"))
    assert acc.codex_sessions_by_account(root) == {"alpha": 1}


def test_unknown_liveness_holds_the_slot_and_termination_releases_it(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    _held_store(
        monkeypatch,
        ("exe-a000000000000001", "connected", "alpha"),
        ("exe-a000000000000002", "degraded", "alpha"),
        ("exe-a000000000000003", "exited", "alpha"),
        ("exe-a000000000000004", "failed", "alpha"),
        ("exe-a000000000000005", "starting", ""),
    )
    assert acc.codex_sessions_by_account(root) == {"alpha": 2, "default": 1}


def test_a_finished_execution_releases_its_slot(tmp_path, monkeypatch):
    from scripts.codex_session import supervisor
    from scripts.codex_session.binding import BindingStore
    from scripts.swarm.store import RedisStore

    root = tmp_path / "proc"
    root.mkdir()
    execution = "exe-c000000000000001"
    store = RedisStore(_held_store(monkeypatch, (execution, "connected", "alpha")))
    record = BindingStore(store).get("sw", execution)
    assert acc.codex_sessions_by_account(root) == {"alpha": 1}
    supervisor.finish(store, "sw", supervisor.Started(record, None, None), 1)
    assert acc.codex_sessions_by_account(root) == {}


def test_an_unreadable_record_holds_a_default_slot(tmp_path, monkeypatch):
    import json

    from scripts.codex_session.binding import RECORDS
    from scripts.swarm.store import RedisStore

    root = tmp_path / "proc"
    root.mkdir()
    redis = _held_store(monkeypatch)
    records = RedisStore(redis).key("sw", RECORDS)
    newer, corrupt, foreign = json.dumps({"schema_version": 2}), "{not json", json.dumps({"schema_version": 1, "x": 1})
    redis.hset(records, mapping={"exe-d002": newer, "exe-d003": corrupt, "exe-d004": foreign, "exe-d005": "[]"})
    assert acc.managed_held() == {(newer, "default"), (corrupt, "default"), (foreign, "default"), ("[]", "default")}
    assert acc.codex_sessions_by_account(root) == {"default": 4}


def test_a_record_on_an_unmanaged_endpoint_keys_by_its_execution(monkeypatch):
    from scripts.codex_session.binding import BindingStore, TransportRecord
    from scripts.swarm.store import RedisStore

    store = RedisStore(_held_store(monkeypatch))
    for execution in ("exe-e001", "exe-e002"):
        record = TransportRecord(
            execution, 1, "eng-1@sw", "engineer@1", "t1", f"unix:///tmp/{execution}", account="beta"
        )
        BindingStore(store).reserve("sw", record)
    assert acc.managed_held() == {("exe-e001", "beta"), ("exe-e002", "beta")}


def test_no_redis_holds_nothing(monkeypatch):
    monkeypatch.setattr("hooks._redis.get_redis", lambda: None)
    assert acc.managed_held() == set()


def test_a_redis_failure_holds_what_the_store_last_held(monkeypatch):
    from redis.exceptions import RedisError

    class Broken:
        def smembers(self, key):
            raise RedisError("down")

    monkeypatch.setattr(acc, "_LAST_HELD", set())
    monkeypatch.setattr("hooks._redis.get_redis", lambda: Broken())
    assert acc.managed_held() == set()
    _held_store(monkeypatch, ("exe-f000000000000001", "connected", "alpha"))
    assert acc.managed_held() == {("f000000000000001", "alpha")}
    monkeypatch.setattr("hooks._redis.get_redis", lambda: Broken())
    assert acc.managed_held() == {("f000000000000001", "alpha")}
