import json
import subprocess
import sys

import pytest

from scripts import gh_audit
from scripts.swarm import github_reads

pytestmark = pytest.mark.unit


@pytest.fixture
def charged(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "AGENTIHOOKS_AGENT_NAME"):
        monkeypatch.delenv(name, raising=False)

    def rows():
        log = github_reads.home() / "charged.jsonl"
        return (
            [(row["caller"], row["endpoint"]) for row in map(json.loads, log.read_text().splitlines())]
            if log.exists()
            else []
        )

    return rows


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["gh", "api", "repos/o/r/actions/runs?head_sha=1"], "repos/o/r/actions/runs"),
        (["/usr/bin/gh", "api", "-X", "POST", "--jq", ".id", "repos/o/r/issues"], "repos/o/r/issues"),
        (["gh", "api", "--method=GET", "repos/o/r/pulls/7"], "repos/o/r/pulls/7"),
        (["gh", "api", "graphql", "-f", "query=x"], None),
        (["gh", "api", "rate_limit"], None),
        (["gh", "api", "/rate_limit"], None),
        (["gh", "api", "/repos/o/r/pulls/7"], "repos/o/r/pulls/7"),
        (["GH_TOKEN=x", "timeout", "20", "gh", "api", "repos/o/r"], "repos/o/r"),
        (["env", "A=1", "command", "gh", "run", "list"], "gh run list"),
        (["echo", "gh", "api", "repos/o/r"], None),
        (["gh", "run", "view", "1", "--log"], "gh run view"),
        (["gh", "workflow", "run", "x.yml"], "gh workflow run"),
        (["gh", "pr", "diff", "7"], "gh pr diff"),
        (["gh", "pr", "view", "7"], None),
        (["gh", "issue", "close", "7"], None),
        (["gh", "api"], None),
        (["git", "api", "repos/o/r"], None),
        ([], None),
    ],
)
def test_endpoint_names_only_core_rest_requests(argv, expected):
    assert gh_audit.endpoint(argv) == expected


def test_agent_shell_charges_each_rest_command_in_a_pipeline(charged):
    command = 'cd x && gh run view 1 --log | grep y; gh api "repos/a/b/pulls/2" 2>&1; gh pr view 2; gh api graphql'
    gh_audit.charge_command("Bash", {"command": command})
    gh_audit.charge_command("Bash", {"command": "cd /repo\ngh api repos/a/b/commits/1\ngit commit -m 'a\ngh api x'"})
    gh_audit.charge_command("Read", {"command": "gh api repos/a/b"})
    gh_audit.charge_command("Bash", {"command": "gh api 'unclosed"})
    gh_audit.charge_command("Bash", "gh api repos/a/b")
    assert charged() == [
        ("agent.bash", "gh run view"),
        ("agent.bash", "repos/a/b/pulls/2"),
        ("agent.bash", "repos/a/b/commits/1"),
    ]


def test_agent_shell_charges_loops_and_nested_shells_to_the_named_agent(charged, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "ci@1-2")
    command = "for n in 1 2; do gh api repos/a/b/runs/$n; done; bash -c 'gh label list'; echo '' | xargs gh run view"
    gh_audit.charge_command("Bash", {"command": command})
    assert charged() == [
        ("agent.ci@1-2", "repos/a/b/runs/$n"),
        ("agent.ci@1-2", "gh label list"),
        ("agent.ci@1-2", "gh run view"),
    ]


def test_process_requests_charge_their_named_caller_or_the_process(charged, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/x/agentihooks", "swarm", "tick"])
    monkeypatch.setitem(sys.modules, "__main__", type(sys)("__main__"))
    gh_audit.audit("subprocess.Popen", ("gh", ["gh", "api", "repos/a/b/commits/1"], None, None))
    with github_reads.calling("swarm.head"):
        gh_audit.audit("subprocess.Popen", (None, ("gh", "run", "list"), None, None))
    gh_audit.audit(
        "subprocess.Popen", ("/bin/sh", ["/bin/sh", "-c", "gh api repos/a/b/tags && gh pr view"], None, None)
    )
    gh_audit.audit("os.exec", ("/usr/bin/gh", ["gh", "run", "watch", "4"], {}))
    gh_audit.audit("os.posix_spawn", ("/usr/bin/gh", ["gh", "workflow", "list"], {}))
    gh_audit.audit("subprocess.Popen", ("gh", "gh api repos/a/b", None, None))
    gh_audit.audit("open", ("gh", ["gh", "api", "repos/a/b"], None, None))
    with gh_audit.uncharged():
        gh_audit.audit("subprocess.Popen", ("gh", ["gh", "api", "repos/a/b/commits/2"], None, None))
    assert charged() == [
        ("proc.agentihooks.swarm", "repos/a/b/commits/1"),
        ("swarm.head", "gh run list"),
        ("proc.agentihooks.swarm", "repos/a/b/tags"),
        ("proc.agentihooks.swarm", "gh run watch"),
        ("proc.agentihooks.swarm", "gh workflow list"),
    ]


def test_process_caller_prefers_the_main_module(monkeypatch):
    main = type(sys)("__main__")
    main.__spec__ = type("Spec", (), {"name": "scripts.doctor.loop.__main__"})()
    monkeypatch.setitem(sys.modules, "__main__", main)
    monkeypatch.setattr(sys, "argv", ["loop", "--once"])
    assert gh_audit.process_caller() == "proc.scripts.doctor.loop"
    monkeypatch.setitem(sys.modules, "__main__", type(sys)("__main__"))
    monkeypatch.setattr(sys, "argv", [])
    assert gh_audit.process_caller() == "proc."


def test_a_real_subprocess_is_charged_once_by_the_installed_hook(charged, tmp_path):
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\necho '{}'\n")
    fake.chmod(0o755)
    with github_reads.calling("swarm.probe"):
        subprocess.run([str(fake), "api", "repos/a/b/pulls/3"], capture_output=True, check=True)
    assert charged() == [("swarm.probe", "repos/a/b/pulls/3")]


def test_install_registers_the_audit_hook(monkeypatch):
    hooks = []
    monkeypatch.setattr(sys, "addaudithook", hooks.append)
    gh_audit.install()
    assert hooks == [gh_audit.audit]


def test_a_charge_records_when_it_was_spent(charged, monkeypatch):
    monkeypatch.setattr(gh_audit.time, "time", lambda: 5.0)
    gh_audit.charge("swarm.probe", "repos/a/b")
    assert json.loads((github_reads.home() / "charged.jsonl").read_text())["at"] == 5.0


def test_a_bare_shell_flag_names_no_request():
    assert gh_audit.endpoints(["sh", "-c"]) == []
    assert gh_audit.endpoints(["sh", "-c", "gh api repos/a/b"]) == ["repos/a/b"]


def test_pre_tool_activity_charges_agent_shell_requests_outside_a_swarm(charged, tmp_path):
    from scripts.swarm.health import activity

    activity.record("Bash", {"command": "gh api repos/a/b/actions/runs/9"}, environ={}, root=tmp_path)
    assert charged() == [("agent.bash", "repos/a/b/actions/runs/9")]
