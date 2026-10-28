import ast
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import authority_context, ledger_authority


def test_trusted_authority_derives_worker_tokens_without_entering_the_environment(monkeypatch):
    from scripts.swarm import runtime
    from scripts.swarm_ledger import ledger

    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    assert authority_context.secret() == secret
    with authority_context.bound(secret):
        assert authority_context.secret() == secret
        assert "SWARM_SECRET" not in os.environ
        expected = ledger_authority.agent_token(secret, "ledger", "worker")
        assert ledger.derived_token("ledger", "worker") == expected
        assert runtime._ledger_token("ledger", "worker") == {"AGENTIHOOKS_LEDGER_AGENT_TOKEN": expected}
    assert authority_context.secret() is None


def source_aliases(tree, package):
    result = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update({item.asname or item.name: item.name for item in node.names})
        elif isinstance(node, ast.ImportFrom):
            parents = package.split(".")
            base = parents[: len(parents) - node.level + 1] if node.level else []
            module = ".".join([*base, node.module or ""]).strip(".")
            if module == "scripts.swarm_ledger.authority_context":
                assert all(item.name != "*" for item in node.names)
            result.update({item.asname or item.name: f"{module}.{item.name}" for item in node.names})
    return result


def source_calls():
    root = Path(__file__).resolve().parents[2]
    result = []
    for folder in (root / "scripts", root / "hooks"):
        for path in folder.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            aliases = source_aliases(tree, ".".join(path.parent.relative_to(root).parts))
            functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            owners = {}
            for index, item in sorted(
                enumerate(functions), key=lambda pair: (pair[1].lineno - pair[1].end_lineno, -pair[0])
            ):
                owners.update(dict.fromkeys(range(item.lineno, item.end_lineno + 1), item.name))
            for node in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
                name = ast.unparse(node.func)
                first, dot, rest = name.partition(".")
                name = aliases.get(first, first) + (dot + rest if dot else "")
                owner = owners.get(node.lineno, "<module>")
                if "__mutmut_" in owner:
                    owner = owner.split("__mutmut_", 1)[0].removeprefix("x_")
                result.append((str(path.relative_to(root)), node.lineno, name, owner))
    return result


def child_launches(calls):
    excluded = {
        "subprocess.CompletedProcess",
        "subprocess.TimeoutExpired",
        "subprocess.CalledProcessError",
        "subprocess.list2cmdline",
        "subprocess.SubprocessError",
    }
    prefixes = (
        "subprocess.",
        "asyncio.create_subprocess",
        "os.spawn",
        "os.posix_spawn",
        "os.exec",
        "os.fork",
        "os.system",
        "os.popen",
    )
    return sorted(
        f"{path}:{line}:{name}" for path, line, name, _ in calls if name.startswith(prefixes) and name not in excluded
    )


def test_every_discovered_child_launch_inherits_no_server_authority(monkeypatch, capsys):
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    calls = source_calls()
    launches = child_launches(calls)
    assert launches
    readers = {
        f"{path}:{owner}" for path, _, name, owner in calls if name == "scripts.swarm_ledger.authority_context.secret"
    }
    assert readers == {"scripts/swarm_ledger/ledger.py:derived_token", "scripts/swarm/runtime.py:_ledger_token"}
    print(json.dumps({"child_launch_paths": launches}))
    code = "import os; from scripts.swarm_ledger import authority_context; print(bool(os.environ.get('SWARM_SECRET') or authority_context.secret()))"
    with authority_context.bound(secret):
        assert "SWARM_SECRET" not in dict(os.environ)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "False"
        with subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=dict(os.environ)
        ) as child:
            assert child.communicate()[0].strip() == "False"
        assert subprocess.check_output([sys.executable, "-c", code], text=True).strip() == "False"
        assert authority_context.secret() == secret
    assert "child_launch_paths" in capsys.readouterr().out


def test_forked_children_cannot_read_private_authority(monkeypatch):
    secret = secrets.token_hex(32)
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    read_end, write_end = os.pipe()
    with authority_context.bound(secret):
        child = os.fork()
        if child == 0:
            os.close(read_end)
            os.write(write_end, b"leaked" if authority_context.secret() else b"clean")
            os.close(write_end)
            os._exit(0)
        os.close(write_end)
        try:
            assert os.read(read_end, 16) == b"clean"
        finally:
            os.close(read_end)
            assert os.waitpid(child, 0)[1] == 0
        assert authority_context.secret() == secret
        authority_context._clear()
        assert authority_context.secret() is None


def test_nested_authority_scopes_restore_memory_without_restoring_exported_secrets(monkeypatch):
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    first, second = secrets.token_hex(32), secrets.token_hex(32)
    with authority_context.bound(first):
        with authority_context.bound(second):
            assert authority_context.secret() == second
        assert authority_context.secret() == first
    assert authority_context.secret() is None


def test_tick_read_and_service_credentials_are_scoped_renewed_and_verified(monkeypatch):
    from scripts.swarm_ledger import ledger

    value = secrets.token_hex(32)
    clock = [1000]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setenv("SWARM_URL", "https://hosted.example")
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_CREDENTIAL", raising=False)
    monkeypatch.setattr(ledger.Who, "from_env", lambda: SimpleNamespace(pinned=False, name=""))
    with authority_context.bound(value):
        headers = ledger.credentials("ledger")
        assert set(headers) == {"X-Ledger-Tick"}
        assert ledger.credentials("ledger", service=True) == headers
        clock[0] += 1
        renewed = ledger.credentials("ledger", service=True)
        assert renewed != headers
    monkeypatch.setenv("SWARM_AUTH_MODE", "edge")
    monkeypatch.setenv("SWARM_SECRET", value)
    assert authority_context.tick_headers("ledger") == {}
    monkeypatch.setattr(ledger_authority, "access_operator", lambda assertion: False)
    assert ledger_authority.caller(headers, "", "ledger") == ""
    assert ledger_authority.caller(headers, "", "another-ledger") is None
    assert ledger_authority.caller(renewed, "", "ledger") == ""
    clock[0] = 1900
    assert ledger_authority.caller(headers, "", "ledger") is None
    assert ledger_authority.caller(renewed, "", "ledger") == ""
    clock[0] = 1000
    monkeypatch.delenv("SWARM_SECRET")
    assert ledger_authority.caller(headers, "", "ledger") is None
    monkeypatch.setenv("SWARM_SECRET", value)
    expires, signature = headers["X-Ledger-Tick"].split(".")
    assert ledger_authority.caller({"X-Ledger-Tick": f"{int(expires) - 1}.{signature}"}, "", "ledger") is None
    future = 1901
    message = f"ledger-tick:ledger:{future}".encode()
    signature = hmac.new(value.encode(), message, hashlib.sha256).hexdigest()
    assert ledger_authority.caller({"X-Ledger-Tick": f"{future}.{signature}"}, "", "ledger") is None
    for invalid in ("", "invalid", "1800.é", ledger_authority.agent_token(value, "ledger", "worker")):
        assert ledger_authority.caller({"X-Ledger-Tick": invalid}, "", "ledger") is None
    assert authority_context.tick_headers("ledger") == {}
    with pytest.raises(SystemExit):
        ledger.credentials("ledger", service=True)


def test_scoped_tick_and_migration_credentials_refuse_cleartext_transport():
    from scripts.swarm_ledger import ledger_link

    for header in ("X-Ledger-Tick", "X-Ledger-Migration"):
        with pytest.raises(ledger_link.Cleartext):
            ledger_link.guard("http://hosted.example", {header: "$FIXTURE_SCOPED_TOKEN"})
        ledger_link.guard("https://hosted.example", {header: "$FIXTURE_SCOPED_TOKEN"})


def test_shared_access_credentials_reach_hosted_clients_without_copying_secrets(monkeypatch):
    from scripts.swarm_ledger import ledger_link

    monkeypatch.delenv("SWARM_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("SWARM_ACCESS_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "$FIXTURE_SHARED_ID")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "$FIXTURE_SHARED_SECRET")
    assert ledger_link.access_headers() == {
        "CF-Access-Client-Id": "$FIXTURE_SHARED_ID",
        "CF-Access-Client-Secret": "$FIXTURE_SHARED_SECRET",
        "User-Agent": "agentihooks-swarm",
    }
    monkeypatch.setenv("SWARM_ACCESS_CLIENT_ID", "$FIXTURE_OVERRIDE_ID")
    monkeypatch.setenv("SWARM_ACCESS_CLIENT_SECRET", "$FIXTURE_OVERRIDE_SECRET")
    assert ledger_link.access_headers()["CF-Access-Client-Id"] == "$FIXTURE_OVERRIDE_ID"
    assert ledger_link.access_headers()["CF-Access-Client-Secret"] == "$FIXTURE_OVERRIDE_SECRET"
    for missing in ("ID", "SECRET"):
        with monkeypatch.context() as absent:
            absent.delenv(f"CF_ACCESS_CLIENT_{missing}")
            absent.delenv(f"SWARM_ACCESS_CLIENT_{missing}")
            assert ledger_link.access_headers() == {}
