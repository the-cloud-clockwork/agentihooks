"""Shared test fixtures for agentihooks."""

import errno
import json
import os
import re
import socket
import sys
import time
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

for _redis_name in list(os.environ):
    if "REDIS" in _redis_name.upper():
        os.environ.pop(_redis_name)

from tests import installer_isolation, ledger_guard, redis_key_guard, swarm_v2_isolation
from tests.shards import (
    FIRST_SHARD_FILES,
    assign_files,
    assign_nodes,
    discover_test_files,
    grouped_files,
    setup_nodes_in_parallel,
    slowest_first,
    source_sizes,
    warm_imports,
)

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

REDIS_PORT = 6379
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_redis(address):
    return isinstance(address, tuple) and address[1:2] == (REDIS_PORT,)


def _refuse_redis_connect(sock, address):
    if _is_redis(address):
        raise ConnectionRefusedError(errno.ECONNREFUSED, f"the test suite refuses Redis at {address[0]}:{REDIS_PORT}")
    return _real_connect(sock, address)


def _refuse_redis_connect_ex(sock, address):
    return errno.ECONNREFUSED if _is_redis(address) else _real_connect_ex(sock, address)


COLLECTED_NODEIDS = pytest.StashKey[list[str]]()
SHARD_FILES = pytest.StashKey[frozenset[str]]()
NODE_SHARDS = pytest.StashKey[dict[str, int]]()
FILE_SHARDS = pytest.StashKey[dict[str, list[int]]]()
WARM_PIDS = pytest.StashKey[list[int]]()


def pytest_collection_modifyitems(config, items):
    if config.getoption("shard") and NODE_SHARDS in config.stash:
        index, shards = (int(part) for part in config.getoption("shard").split("/"))
        selected = []
        for item in items:
            node = re.sub(r"@[^\[\]]*$", "", item.nodeid)
            owners = config.stash[FILE_SHARDS].get(node.split("::", 1)[0], range(shards))
            fallback = owners[zlib.crc32(node.encode()) % len(owners)]
            shard = config.stash[NODE_SHARDS].get(node, fallback)
            if shard == index - 1:
                selected.append(item)
        items[:] = selected
    if hasattr(config, "workerinput"):
        durations = json.loads((config.rootpath / ".test_durations").read_text())
        order = {nodeid: i for i, nodeid in enumerate(slowest_first([item.nodeid for item in items], durations, 0.1))}
        items.sort(key=lambda item: order[item.nodeid])
    config.stash[COLLECTED_NODEIDS] = [item.nodeid for item in items]


def pytest_addoption(parser):
    parser.addoption("--shard", default=None, metavar="N/M", help="collect only the test files of shard N of M")


def _shard_files(config) -> frozenset[str]:
    if SHARD_FILES not in config.stash:
        index, shards = (int(part) for part in config.getoption("shard").split("/"))
        durations = json.loads((config.rootpath / ".test_durations").read_text())
        discovered = discover_test_files(config.rootpath)
        pinned = FIRST_SHARD_FILES.intersection(discovered)
        files = [path for path in discovered if path not in pinned]
        workers = (
            getattr(config, "workerinput", {}).get("workercount")
            or getattr(getattr(config, "option", None), "numprocesses", None)
            or 1
        )
        if workers == "auto":
            workers = os.cpu_count() or 1
        if workers > 1:
            measured = {node: seconds for node, seconds in durations.items() if node.split("::", 1)[0] in files}
            parts = assign_nodes(measured, shards, grouped_files(config.rootpath, files), workers)
            config.stash[NODE_SHARDS] = {node: shard for shard, part in enumerate(parts) for node in part}
            owners = {}
            for node, shard in config.stash[NODE_SHARDS].items():
                owners.setdefault(node.split("::", 1)[0], set()).add(shard)
            config.stash[FILE_SHARDS] = {path: sorted(shards) for path, shards in owners.items()}
            config.stash[FILE_SHARDS].update({path: [0] for path in pinned})
            known = {node.split("::", 1)[0] for node in measured}
            files = {node.split("::", 1)[0] for node in parts[index - 1]} | (set(files) - known)
        else:
            files = assign_files(durations, files, shards, source_sizes(config.rootpath, files))[index - 1]
        config.stash[SHARD_FILES] = frozenset(files) | (pinned if index == 1 else frozenset())
    return config.stash[SHARD_FILES]


def pytest_configure(config):
    config.args.sort(key=lambda arg: (Path(arg.split("::", 1)[0]).parent.parts, arg))
    workers = getattr(config.option, "numprocesses", None)
    if not config.getoption("shard") or not workers or hasattr(config, "workerinput") or not hasattr(os, "fork"):
        return
    from xdist.workermanage import NodeManager

    NodeManager.setup_nodes = setup_nodes_in_parallel
    modules = [path.removesuffix(".py").replace("/", ".") for path in sorted(_shard_files(config))]
    config.stash[WARM_PIDS] = warm_imports(modules, workers)


def pytest_unconfigure(config):
    for pid in config.stash.get(WARM_PIDS, []):
        os.waitpid(pid, 0)


def pytest_ignore_collect(collection_path, config):
    spec = config.getoption("shard")
    if not spec or collection_path.suffix != ".py" or not collection_path.name.startswith("test_"):
        return None
    if collection_path.relative_to(config.rootpath).as_posix() not in _shard_files(config):
        return True
    return None


@pytest.fixture(autouse=True)
def _isolate_real_user_paths(tmp_path, monkeypatch, request):
    """Redirect every real-home path away from the developer's machine.

    `install.py` reaches the real home through more routes than the obvious
    globals, and they are hit transitively — `_install_system_prompt` →
    `_record_managed_claude_md` → `_save_state` — so a test that never mentions
    any of them still writes real files. This has already destroyed a real
    `~/.claude/CLAUDE.md` and uninstalled the real CLI once each.

    `Path.home` itself is patched, not just the derived symbols. That is the only
    thing that closes call sites which build the path inline — notably
    `_migrate_profile_rename`, which does `Path.home() / ".claude.json"` as a raw
    literal and so cannot be neutralised by patching module attributes. The
    derived globals are then re-pointed for the modules that bound them at import.

    Autouse and suite-wide on purpose: opting in per file is how the gap
    reappeared last time.
    """
    real_home = Path.home()
    fake_home = tmp_path / "_home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".agentihooks").mkdir(parents=True)
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".copilot").mkdir(parents=True)
    (fake_home / ".agents" / "skills").mkdir(parents=True)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("LIFECYCLE_GC_ENABLED", "false")
    # Every swarm tick sweeps the launch run folder and the herdr server.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "_run"))
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "_run" / "herdr.sock"))
    monkeypatch.delenv("HERDR_CONFIG_PATH", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "_kube" / "config"))
    monkeypatch.delenv("VAULT_ROOT", raising=False)
    monkeypatch.setattr(swarm_v2_isolation, "FIXTURE_ROOT", tmp_path.resolve())
    # CI has no herdr; a test that needs one installs a fake.
    monkeypatch.setattr("scripts.herdr_host.binary", lambda: None)
    monkeypatch.setattr("scripts.herdr_setup.binary", lambda: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # CODEX_HOME is read BEFORE Path.home() by targets.codex_target.codex_home,
    # so patching Path.home does not cover it. Unset today on the developer's
    # machine, which is luck, not isolation — an operator who exports it (the
    # installer supports a comma list) would have the whole suite writing into
    # their real codex install.
    for name in (
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_HOME_DIR",
        "AGENTIHOOKS_CLAUDE_HOME",
        "AGENTIHOOKS_PROFILE",
        "AGENTIHOOKS_PROFILE_REPORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # Same for COPILOT_HOME, read first by targets.copilot_target.copilot_home.
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    # AGENTIHOOKS_HOME drives the state dir, which the copilot adapter's managed
    # env file lives in — and that path is resolved INDEPENDENTLY of COPILOT_HOME.
    # Isolating only the target home therefore still writes, and on the
    # no-directives branch DELETES, the operator's real ~/.agentihooks/copilot.env.
    # That has already happened once.
    monkeypatch.delenv("AGENTIHOOKS_HOME", raising=False)
    # A suite run from a swarm agent's shell inherits its swarm, seat, task and routed account.
    for _inherited in [name for name in os.environ if name.startswith("AGENTIHOOKS_")]:
        monkeypatch.delenv(_inherited)
    # AGENTIBRAIN_HOME — and its Path.home()-derived default — is where the brain
    # keeps its own .env. hooks.config adopts BRAIN_URL / KB_ROUTER_TOKEN from that
    # file, and it does so at IMPORT, which happens at collection before any fixture
    # runs. An unisolated suite therefore reads the operator's live bearer into
    # os.environ once and keeps it for the whole session, where a single mismatched
    # assertion renders it into the diff. Patching Path.home afterwards is too late,
    # so the keys are cleared per test as well.
    monkeypatch.setenv("AGENTIBRAIN_HOME", str(fake_home / ".agentibrain"))
    for _adopted in ("KB_ROUTER_TOKEN", "BRAIN_URL", "BRAIN_HTTP_TOKEN"):
        monkeypatch.delenv(_adopted, raising=False)
    # Every PreToolUse delivers the running session's inbox from the swarm Redis.
    # A missing socket file fails at once; some hosts drop a connect to an unbound port until the timeout.
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", f"unix://{tmp_path / 'no-swarm-redis.sock'}")
    # The workbench Redis on the default port holds live swarms and inboxes, and the shell's
    # REDIS_URL names a remote one; a test that reaches either can wipe real state.
    monkeypatch.setattr(socket.socket, "connect", _refuse_redis_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse_redis_connect_ex)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr("hooks._redis._redis_client", None)
    monkeypatch.setattr("hooks._redis._redis_checked", False)
    monkeypatch.setenv("REDIS_KEY_PREFIX", swarm_v2_isolation.RUN_PREFIX)
    for _prefixed in (
        "hooks._redis._KEY_PREFIX",
        "hooks.memory.store._KEY_PREFIX",
        "hooks.observability.event_relay.STREAM_KEY_PREFIX",
    ):
        monkeypatch.setattr(_prefixed, swarm_v2_isolation.RUN_PREFIX)
    # The workbench shell and ~/.agentihooks/*.env carry the live collector and
    # Langfuse settings; only real sessions may report to them.
    for _telemetry in (
        "AGENTIHOOKS_OTLP_ENDPOINT",
        "AGENTIHOOKS_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "AGENTIHOOKS_OTEL_COLLECTOR",
        "AGENTIHOOKS_LANGFUSE_ENABLED",
        "OTEL_LANGFUSE_ENABLED",
        "OTEL_LANGFUSE_ENDPOINT",
        "OTEL_LANGFUSE_HOST_HEADER",
        "OTEL_LANGFUSE_PUBLIC_KEY",
        "OTEL_LANGFUSE_SECRET_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
    ):
        monkeypatch.delenv(_telemetry, raising=False)
    monkeypatch.setattr("hooks.config.OTEL_LANGFUSE_ENABLED", False)
    for _bound in (
        "OTEL_LANGFUSE_ENDPOINT",
        "OTEL_LANGFUSE_HOST_HEADER",
        "OTEL_LANGFUSE_PUBLIC_KEY",
        "OTEL_LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.setattr(f"hooks.config.{_bound}", "")

    # hooks.config binds AGENTIHOOKS_HOME at import, so these would otherwise run
    # the operator's real condition scripts and write the real cache and counters.
    fake_state_dir = fake_home / ".agentihooks"
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", fake_state_dir)
    # A stale stamp makes SessionStart spawn a detached `agentihooks deps ensure` that mutmut's runner then reaps.
    (fake_state_dir / "deps.stamp").write_text(json.dumps({"ok_at": time.time()}))
    monkeypatch.setattr("hooks.config.LOG_FILE", str(fake_state_dir / "logs" / "hooks.log"))
    monkeypatch.setattr("hooks.common.LOG_FILE", str(fake_state_dir / "logs" / "hooks.log"))
    monkeypatch.setattr("hooks.config.BROADCAST_FILE", str(fake_state_dir / "broadcast.json"))
    monkeypatch.setattr("hooks.context.broadcast.BROADCAST_FILE", str(fake_state_dir / "broadcast.json"))
    monkeypatch.setattr(
        "hooks.config.BROADCAST_DELIVERY_STATE_FILE", str(fake_state_dir / "broadcast_delivery_state.json")
    )
    monkeypatch.setattr(
        "hooks.context.broadcast.BROADCAST_DELIVERY_STATE_FILE", str(fake_state_dir / "broadcast_delivery_state.json")
    )
    monkeypatch.setattr("hooks.context.quota_policy.AGENTIHOOKS_HOME", fake_state_dir)
    monkeypatch.setattr("hooks.observability.agent_trace.CURSOR_DIR", fake_state_dir / "agent_trace")
    monkeypatch.setattr("hooks.context.brain_adapter._HASH_CACHE_FILE", fake_state_dir / "brain_feed_hash")
    monkeypatch.setattr("hooks.context.profile_chain.state_path", lambda: fake_state_dir / "state.json")
    monkeypatch.setattr("hooks.context.conditions._cache_path", lambda *a: fake_state_dir / "cache" / "conditions.json")
    monkeypatch.setattr("hooks.context.conditions.runtime_dir", lambda: fake_state_dir / "conditions")
    for _name, _file in (
        ("_match_counter_path", "enforcement_match_counters.json"),
        ("_counter_path", "enforcement_counters.json"),
        ("_delivery_path", "enforcement_delivery_state.json"),
        ("_store_path", "enforcements.json"),
    ):
        monkeypatch.setattr(f"hooks.context.enforcement.{_name}", lambda f=_file: fake_state_dir / f)
    monkeypatch.setattr("hooks.config.CONDITIONS_ENABLED", False)
    from hooks.targets import emitter

    monkeypatch.setattr(emitter, "_forced", False)
    monkeypatch.setattr(emitter, "_buffer", [])
    monkeypatch.setattr(emitter, "_hook_fields", {})
    monkeypatch.setattr(emitter, "_top_fields", {})

    import install

    import scripts.install as package_install

    installer_paths = {
        "CLAUDE_HOME": fake_home / ".claude",
        "AGENTIHOOKS_STATE_DIR": fake_state_dir,
        "STATE_JSON": fake_state_dir / "state.json",
        "_CLAUDE_JSON": fake_home / ".claude.json",
        "_BASHRC": fake_home / ".bashrc",
        "_ENV_FILE_DST": fake_state_dir / ".env",
        "_SYNC_LOCK_FILE": fake_state_dir / "sync.lock",
        "AGENTIHOOKS_ROOT": tmp_path / "_repo",
    }
    for module in (install, package_install):
        for name, value in installer_paths.items():
            monkeypatch.setattr(module, name, value)
            assert Path(getattr(module, name)).resolve().is_relative_to(tmp_path.resolve()), (
                f"{module.__name__}.{name} escapes the test directory — refusing to run"
            )
    monkeypatch.setattr(installer_isolation, "WRITE_ROOT", tmp_path.resolve())
    monkeypatch.setattr(installer_isolation, "PROTECTED_PATHS", swarm_v2_isolation.LIVE_ROOTS)
    monkeypatch.setattr("scripts.deps_preflight.manifest_path", lambda: None)
    # Same reasoning for the env-var bundle: `_managed_roots()` takes it verbatim,
    # so a developer with it exported would run a different suite than CI.
    monkeypatch.delenv("AGENTIHOOKS_BUNDLE_PATH", raising=False)

    assert Path.home() != real_home, "Path.home() still returns the real home — refusing to run"

    # Codex and copilot write through their own resolvers, not install.py
    # globals, so the loop above cannot see them. Assert the same refusal bar.
    from targets._common import agents_skills_home
    from targets.codex_target import codex_home
    from targets.copilot_target import CopilotAdapter, copilot_home

    from hooks import config as hooks_config
    from hooks.config import _agentibrain_home
    from scripts.claude_config import claude_home, claude_json
    from scripts.herdr_setup import config_path as herdr_config_path

    for label, value in (
        ("claude_home", claude_home()),
        ("claude_json", claude_json()),
        ("agentihooks home", hooks_config.AGENTIHOOKS_HOME),
        ("codex_home", codex_home()),
        ("copilot_home", copilot_home()),
        ("agents_skills_home", agents_skills_home()),
        ("copilot managed env file", CopilotAdapter._bypass_env_file()),
        ("brain home", _agentibrain_home()),
        ("herdr config", herdr_config_path()),
        ("kubeconfig", Path(os.environ["KUBECONFIG"])),
    ):
        assert not any(value.resolve().is_relative_to(root) for root in installer_isolation.PROTECTED_PATHS), (
            f"{label}() still resolves to a live install path ({value}) — refusing to run"
        )
        swarm_v2_isolation.confine(tmp_path, label, value)
    before = swarm_v2_isolation.REJECTIONS.copy()
    yield
    delta = swarm_v2_isolation.REJECTIONS - before
    if delta:
        request.node.user_properties.append((swarm_v2_isolation.METRIC, json.dumps(dict(sorted(delta.items())))))


@pytest.fixture(autouse=True)
def _real_ledger_folder_guard():
    before = len(ledger_guard.touched)
    yield
    assert ledger_guard.touched[before:] == [], "this test reached the operator's real ledger folder"


@pytest.fixture(autouse=True)
def _production_redis_key_guard(monkeypatch):
    # The isolation fixture above drops every AGENTIHOOKS_ variable; subprocesses need the prefix back.
    monkeypatch.setenv(redis_key_guard.keyspace.ENV, redis_key_guard.keyspace.ROOT)
    before = len(redis_key_guard.written)
    yield
    assert redis_key_guard.written[before:] == [], "this test wrote a production swarm Redis key"


@pytest.fixture
def ledger_port():
    with ledger_guard.reserve_port() as hold:
        yield hold.getsockname()[1]


@pytest.fixture(autouse=True)
def _swarm_codes_in_order(monkeypatch):
    """Each test's swarms get codes a1b2c3, a1b2c4, ... in creation order, so agent names are known in advance."""
    from itertools import count

    from scripts.swarm import naming

    codes = count(0xA1B2C3)
    monkeypatch.setattr(naming, "_mint", lambda: f"{next(codes):06x}")


@pytest.fixture(autouse=True)
def _swarm_runs_as_installed(monkeypatch):
    from scripts.swarm import timer

    monkeypatch.setattr(timer, "_roots", lambda: (Path("/installed"), Path("/installed")))


@pytest.fixture(autouse=True)
def _ci_speed_offline(request, monkeypatch):
    from scripts.swarm import ci_speed

    if not getattr(request.module, "CI_SPEED_READ", False):
        monkeypatch.setattr(ci_speed, "read_runs", lambda *args, **kwargs: [])


@pytest.fixture(autouse=True)
def _task_sizing_offline(monkeypatch):
    from hooks.classifier import ClassifierUnavailable
    from scripts.swarm import difficulty

    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("classifier disabled in unit tests")

    monkeypatch.setattr(difficulty, "decide", unavailable)


@pytest.fixture(autouse=True)
def _task_grouping_offline(monkeypatch):
    from hooks.classifier import ClassifierUnavailable
    from scripts.swarm import grouping

    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("classifier disabled in unit tests")

    monkeypatch.setattr(grouping, "decide", unavailable)


@pytest.fixture(autouse=True)
def _ledger_duplicates_offline(monkeypatch):
    from hooks.classifier import ClassifierUnavailable
    from scripts.swarm_ledger import ledger_duplicates, ledger_task_duplicates

    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("classifier disabled in unit tests")

    monkeypatch.setattr(ledger_duplicates, "decide", unavailable)
    child = (sys.executable, "-m", "tests.swarm_ledger.duplicate_child", '[[], "unavailable", 0]')
    monkeypatch.setattr(ledger_task_duplicates, "CHILD", child)


@pytest.fixture
def mock_env():
    """Provide a clean environment for tests."""
    env = {
        "CLAUDE_HOOK_LOG_ENABLED": "true",
        "CLAUDE_HOOK_LOG_FILE": "/tmp/test-hooks.log",
    }
    with patch.dict(os.environ, env, clear=False):
        yield env


@pytest.fixture
def tmp_log_file(tmp_path):
    """Provide a temporary log file path."""
    return tmp_path / "test.log"


@pytest.fixture
def sample_transcript_entry():
    """A sample transcript JSONL entry."""
    return {
        "type": "user",
        "message": {"content": [{"type": "text", "text": "Hello Claude"}]},
        "timestamp": "2026-01-15T10:00:00Z",
        "uuid": "test-uuid-001",
    }


@pytest.fixture
def sample_tool_use_event():
    """A sample PreToolUse hook event."""
    return {
        "session_id": "test-session",
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/test.txt", "content": "hello"},
    }
