import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.swarm_ledger import page_policy

SELF_ONLY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
)
LIVE = (
    "default-src 'none'; script-src 'self' http://localhost:8400; style-src 'self'; img-src 'self'; "
    "connect-src 'self' http://localhost:8400; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
)
ON = {"LEDGER_IMPECCABLE_LIVE": "1"}
ROOT = Path(__file__).resolve().parents[2]


def test_scratch_server_with_the_setting_allows_the_live_origin(tmp_path):
    assert page_policy.policy(ON, tmp_path, 8799) == LIVE


def test_scratch_server_without_the_setting_keeps_self_only(tmp_path):
    assert page_policy.policy({}, tmp_path, 8799) == SELF_ONLY


def test_only_the_value_one_turns_the_setting_on(tmp_path):
    for value in ("", "0", "true", "yes", "11"):
        assert page_policy.policy({"LEDGER_IMPECCABLE_LIVE": value}, tmp_path, 8799) == SELF_ONLY


def test_shared_folder_refuses_the_setting_on_any_port(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    shared = tmp_path / "development-ledger"
    shared.mkdir()
    assert page_policy.policy(ON, shared, 8765) == SELF_ONLY
    assert page_policy.policy(ON, shared, 8799) == SELF_ONLY
    assert page_policy.policy(ON, tmp_path / "x" / ".." / "development-ledger", 8799) == SELF_ONLY


def test_fixed_port_refuses_the_setting_in_a_scratch_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert page_policy.policy(ON, tmp_path, 8765) == SELF_ONLY
    assert page_policy.policy(ON, tmp_path, 8764) == LIVE
    assert page_policy.policy(ON, tmp_path, 8766) == LIVE


def test_ledger_server_sends_the_policy_built_from_its_folder_and_port():
    from scripts.swarm_ledger import ledger_server

    assert ledger_server.PAGE_HEADERS["Content-Security-Policy"] == ledger_server.PAGE_POLICY
    assert ledger_server.PAGE_POLICY == page_policy.policy(
        ledger_server.os.environ, ledger_server.core.LEDGER_DIR, ledger_server.PORT
    )


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"LEDGER_IMPECCABLE_LIVE": "1", "LEDGER_DIR": "scratch", "LEDGER_PORT": "8799"}, LIVE),
        ({"LEDGER_DIR": "scratch", "LEDGER_PORT": "8799"}, SELF_ONLY),
        ({"LEDGER_IMPECCABLE_LIVE": "1", "LEDGER_PORT": "8799"}, SELF_ONLY),
    ],
)
def test_ledger_server_policy_follows_its_environment(tmp_path, env, expected):
    if "LEDGER_DIR" in env:
        env = {**env, "LEDGER_DIR": str(tmp_path / env["LEDGER_DIR"])}
    run = subprocess.run(
        [sys.executable, "-c", "from scripts.swarm_ledger import ledger_server; print(ledger_server.PAGE_POLICY)"],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": str(ROOT), **env},
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert run.stdout == expected + "\n"
