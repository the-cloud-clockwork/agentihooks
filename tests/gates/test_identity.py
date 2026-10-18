"""The identity pin: a swarm session cannot act as another agent."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.gates import entry
from scripts.gates.base import Call, Decision, Gate, Who
from scripts.gates.identity import PinnedIdentity, refusal

ROOT = Path(__file__).resolve().parents[2]
ME, OTHER, SLUG = "engineer@1-1", "engineer@1-2", "demo"
SWARM_ENV = {"AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": ME}


@pytest.fixture(autouse=True)
def no_redis():
    with patch("hooks._redis.get_redis", return_value=None):
        yield


def bash(command):
    return Call(tool="Bash", tool_input={"command": command})


def decide(command, env=SWARM_ENV):
    return PinnedIdentity().decide(bash(command), Who.from_env(env), None)


class TestWho:
    def test_reads_the_swarm_identity(self):
        env = {**SWARM_ENV, "AGENTIHOOKS_SWARM_LANE": "eng", "AGENTIHOOKS_SWARM_TASK": "g02"}
        who = Who.from_env({**env, "AGENTIHOOKS_PROFILE": "engineer"})
        assert who == Who(name=ME, swarm=SLUG, lane="eng", task="g02", profile="engineer")
        assert who.pinned

    @pytest.mark.parametrize("env", [{}, {"AGENTIHOOKS_SWARM": SLUG}, {"AGENTIHOOKS_AGENT_NAME": ME}])
    def test_not_pinned_without_both_swarm_and_name(self, env):
        assert not Who.from_env(env).pinned


class TestRefusal:
    def test_own_name_passes(self):
        assert refusal(ME, Who.from_env(SWARM_ENV)) == ""

    def test_no_name_passes(self):
        assert refusal("", Who.from_env(SWARM_ENV)) == ""

    def test_other_name_is_refused_with_the_fix(self):
        text = refusal(OTHER, Who.from_env(SWARM_ENV))
        assert text == (
            f"this session is {ME} in swarm {SLUG} and cannot act as {OTHER}: run the command with --as {ME}"
        )

    def test_outside_a_swarm_any_name_passes(self):
        assert refusal(OTHER, Who.from_env({"AGENTIHOOKS_AGENT_NAME": ME})) == ""

    def test_alias_of_own_name_passes(self):
        with patch("scripts.swarm.naming.resolve_name", side_effect=lambda n: ME if n == "old-eng-1" else n):
            assert refusal("old-eng-1", Who.from_env(SWARM_ENV)) == ""


class TestGateInterface:
    def test_identity_is_a_gate(self):
        gate = PinnedIdentity()
        assert isinstance(gate, Gate)
        assert gate.name == "identity"

    def test_call_from_payload(self):
        call = Call.from_payload({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert call == Call(tool="Bash", tool_input={"command": "ls"})
        assert call.command == "ls"

    def test_call_from_empty_payload(self):
        call = Call.from_payload({"tool_input": None})
        assert (call.tool, call.tool_input, call.command) == ("", {}, "")

    def test_decision_deny(self):
        assert Decision.deny("no") == Decision(allowed=False, reason="no")
        assert Decision().allowed


class TestMatches:
    @pytest.mark.parametrize(
        "call",
        [
            Call(tool="Bash", tool_input={"command": "agentihooks ledger --slug x --as y say hi"}),
            Call(tool="Bash", tool_input={"command": "bash -c 'agentihooks msg inbox'"}),
        ],
    )
    def test_matches_bash_running_agentihooks(self, call):
        assert PinnedIdentity().matches(call)

    @pytest.mark.parametrize(
        "call",
        [
            Call(tool="Bash", tool_input={"command": "git status"}),
            Call(tool="Edit", tool_input={"command": "agentihooks ledger"}),
        ],
    )
    def test_ignores_other_calls(self, call):
        assert not PinnedIdentity().matches(call)


class TestDecide:
    @pytest.mark.parametrize(
        "command",
        [
            f"agentihooks ledger --slug {SLUG} --as {OTHER} comment phases/p8 hi",
            f"agentihooks swarm {SLUG} --as {OTHER} done --pr x",
            f"agentihooks swarm {SLUG} --as={OTHER} say hi",
            f"agentihooks ledger --slug {SLUG} --a {OTHER} say hi",
            f"cd ~ && agentihooks ledger --slug {SLUG} --as {OTHER} say hi",
            f"bash -c 'agentihooks ledger --slug {SLUG} --as {OTHER} say hi'",
            f"/usr/bin/agentihooks msg --as {OTHER} inbox",
            f"bash -x -c 'agentihooks ledger --slug {SLUG} --as {OTHER} say hi'",
            f"eval 'agentihooks ledger --slug {SLUG} --as {OTHER} say hi'",
            f"agentihooks swarm {SLUG} say hi --as {OTHER}",
            f"agentihooks ledger --slug {SLUG} --as {OTHER} say -n",
            f"env -C /tmp agentihooks msg --as {OTHER} inbox",
            f"timeout 30 agentihooks msg --as {OTHER} inbox",
        ],
    )
    def test_denies_another_agents_name(self, command):
        decision = decide(command)
        assert not decision.allowed
        assert decision.reason == refusal(OTHER, Who.from_env(SWARM_ENV))

    @pytest.mark.parametrize(
        "command",
        [
            f"agentihooks ledger --slug {SLUG} --as {ME} say hi",
            f"agentihooks swarm {SLUG} done --pr x",
            "agentihooks msg inbox",
            f"agentihooks claude --as {OTHER}",
            f"agentihooks ledger --slug {SLUG} --as {ME} say '--as {OTHER}'",
            f"echo agentihooks ledger --as {OTHER}",
            "agentihooks",
            f"git commit -m 'agentihooks ledger --slug {SLUG} --as {OTHER} say'",
            f"mytool swarm {SLUG} --as {OTHER}; agentihooks msg inbox",
            "env; agentihooks msg inbox",
            f"agentihooks ledger --slug {SLUG} --as",
        ],
    )
    def test_allows_own_name_and_other_commands(self, command):
        assert decide(command) == Decision()

    def test_the_whole_name_is_read(self):
        decision = decide(f"agentihooks ledger --slug {SLUG} --as eXtra@1-2 say hi")
        assert decision.reason == refusal("eXtra@1-2", Who.from_env(SWARM_ENV))

    @pytest.mark.parametrize(
        "command",
        [
            f"AGENTIHOOKS_AGENT_NAME={OTHER} agentihooks msg send master hi",
            f"AGENTIHOOKS_SWARM= agentihooks ledger --slug {SLUG} --as {OTHER} say hi",
            f"export AGENTIHOOKS_AGENT_NAME={OTHER}; agentihooks msg inbox",
            "unset AGENTIHOOKS_SWARM; agentihooks ledger --slug x --as y say hi",
            "unset -v AGENTIHOOKS_AGENT_NAME && agentihooks msg inbox",
            "env -u AGENTIHOOKS_SWARM agentihooks swarm x --as y say hi",
            "env -uAGENTIHOOKS_AGENT_NAME agentihooks msg inbox",
            "env --unset=AGENTIHOOKS_SWARM agentihooks swarm x say hi",
            "env --unset AGENTIHOOKS_SWARM agentihooks swarm x say hi",
            "env -i agentihooks swarm x --as y say hi",
            "env --ignore-environment agentihooks msg inbox",
            "env - agentihooks msg inbox",
            "declare -x AGENTIHOOKS_AGENT_NAME=y; agentihooks msg inbox",
        ],
    )
    def test_denies_rewriting_the_identity_variables(self, command):
        decision = decide(command)
        assert not decision.allowed
        assert decision.reason == (
            f"this session is {ME} in swarm {SLUG}: a command that changes AGENTIHOOKS_AGENT_NAME or "
            "AGENTIHOOKS_SWARM cannot run agentihooks ledger, swarm or msg. Run it without the change."
        )

    @pytest.mark.parametrize(
        "command",
        [
            "AGENTIHOOKS_SWARM= agentihooks claude",
            "echo $AGENTIHOOKS_AGENT_NAME; agentihooks msg inbox",
            "printenv AGENTIHOOKS_AGENT_NAME; agentihooks msg inbox",
            "env FOO=1 agentihooks msg inbox",
            "env -C /tmp agentihooks msg inbox",
        ],
    )
    def test_allows_reading_or_other_commands(self, command):
        assert decide(command) == Decision()

    def test_outside_a_swarm_nothing_is_denied(self):
        env = {"AGENTIHOOKS_AGENT_NAME": ME}
        assert decide(f"AGENTIHOOKS_SWARM= agentihooks ledger --slug x --as {OTHER} say hi", env) == Decision()

    def test_unbalanced_quotes_still_checked(self):
        assert not decide(f"agentihooks ledger --slug x --as {OTHER} say 'hi").allowed


class TestEntry:
    def run(self, argv, payload, env=SWARM_ENV):
        err = io.StringIO()
        with patch("sys.stderr", err):
            code = entry.main(argv, io.StringIO(json.dumps(payload)), env)
        return code, err.getvalue()

    def test_deny_exits_two_with_reason(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": f"agentihooks ledger --slug x --as {OTHER} say"}}
        assert self.run(["identity"], payload) == (2, refusal(OTHER, Who.from_env(SWARM_ENV)) + "\n")

    def test_allow_exits_zero_silently(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": f"agentihooks ledger --slug x --as {ME} say"}}
        assert self.run(["identity"], payload) == (0, "")

    def test_unmatched_call_exits_zero(self):
        assert self.run(["identity"], {"tool_name": "Read", "tool_input": {}}) == (0, "")

    def test_empty_stdin_exits_zero(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            assert entry.main(["identity"], io.StringIO(""), SWARM_ENV) == 0

    @pytest.mark.parametrize("argv", [[], ["nope"]])
    def test_unknown_gate_exits_one(self, argv):
        code, err = self.run(argv, {})
        assert code == 1
        assert (
            err
            == "agentihooks gate: name one gate of: build, claim-stop, identity, intent, one-push, placement, prompts, push-stop, quiet, reruns, subagents, watch\n"
        )

    def test_unknown_gate_lists_every_gate(self, monkeypatch):
        monkeypatch.setattr(entry, "GATES", {"b": PinnedIdentity(), "a": PinnedIdentity()})
        monkeypatch.setattr(entry, "HOST_GATES", {"c": PinnedIdentity()})
        assert self.run(["nope"], {}) == (1, "agentihooks gate: name one gate of: a, b, c\n")

    def test_defaults_read_process_stdin_and_environment(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["gate", "identity"])
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {}})))
        for key, value in SWARM_ENV.items():
            monkeypatch.setenv(key, value)
        assert entry.main() == 0

    def test_registry_names_each_gate(self):
        assert sorted(entry.GATES) == [
            "build",
            "claim-stop",
            "identity",
            "intent",
            "one-push",
            "push-stop",
            "quiet",
            "reruns",
            "subagents",
            "watch",
        ]
        assert all(name == gate.name for name, gate in entry.GATES.items())
        assert isinstance(entry.GATES["identity"], PinnedIdentity)


class TestModuleEntry:
    def test_python_m_scripts_gates_denies(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": f"agentihooks swarm x --as {OTHER} say hi"}}
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.gates", "identity"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={**os.environ, **SWARM_ENV, "REDIS_URL": "redis://127.0.0.1:1/0"},
            timeout=60,
        )
        assert proc.returncode == 2, proc.stderr
        assert f"cannot act as {OTHER}" in proc.stderr
