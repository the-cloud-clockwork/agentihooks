"""A gate condition whose child cannot import the gates package is reported by the healthy hook process that ran it:
the call goes through, a fail-open row names the gate and the import error, and one setup alert per gate per hour
names the gate and the interpreter."""

import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from hooks.context import conditions, profile_chain
from scripts.gates import log
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api.client import ResourceClient

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "profiles" / "package" / "roles" / "engineer" / ".claude" / "conditions"
RERUNS = ROLE / "pre-bash.gh-rerun_budget.py"
IDENTITY = next(ROLE.glob("pre-*-identity_pin.py"))
SLUG, ME, TASK = "sw", "engineer@1-2", "t2"
START, HOUR = 1_800_000_000.0, 3600
IMPORTED = 'from scripts.gates.entry import main\n\nraise SystemExit(main(["reruns"]))\n'


class World:
    def __init__(self, directory: Path) -> None:
        self.directory, self.now, self.sent = directory, START, []

    def clock(self) -> float:
        return self.now

    def transport(self, slug: str, path: str, body: dict | None) -> dict:
        if body is None:
            return {"revision": 1}
        self.sent.extend(body["ops"])
        return {"revision": 2}

    def place(self, source: Path) -> None:
        shutil.copyfile(source, self.directory / source.name)

    def write(self, name: str, body: str) -> None:
        (self.directory / name).write_text(body)

    def run(self, command: str) -> conditions.StepResult:
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "sid-gate-import",
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "probe"},
            "permission_mode": "bypassPermissions",
            "cwd": str(self.directory.parent),
        }
        return conditions.run_step("pre", payload)

    def rows(self) -> list[dict]:
        path = log.gate_log_path(SLUG)
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def items(self) -> list[str]:
        return [op["item"] for op in self.sent]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("HOME", str(tmp_path))
    bundle = tmp_path / "bundle"
    directory = bundle / ".claude" / "conditions"
    directory.mkdir(parents=True)
    state = profile_chain.state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"bundle": {"path": str(bundle)}, "targets": {"global": {"claude": {"profile": "alpha"}}}})
    )
    broken = tmp_path / "broken" / "scripts"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("")
    for name, value in {
        "AGENTIHOOKS_TARGET": "claude",
        "PYTHONPATH": str(broken.parent),
        "AGENTIHOOKS_SWARM": SLUG,
        "AGENTIHOOKS_AGENT_NAME": ME,
        "AGENTIHOOKS_SWARM_TASK": TASK,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("hooks.config.CONDITIONS_ENABLED", True)
    held = World(directory)
    monkeypatch.setattr(time, "time", held.clock)
    monkeypatch.setattr(
        ledger, "client", lambda slug, timeout=10: ResourceClient("http://ledger.invalid", {}, timeout, held.transport)
    )
    return held


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_rerun_budget_shim_that_cannot_import_the_gates_is_reported_once_per_hour(world):
    world.place(RERUNS)
    result = world.run("gh run rerun 7")
    assert result.decision is None and result.input_patch == {}
    assert "failed" in result.contexts[0]
    rows = world.rows()
    assert [(row["gate"], row["kind"], row["agent"], row["task"], row["tool"]) for row in rows] == [
        ("reruns", "fail-open", ME, TASK, "Bash")
    ]
    assert "ModuleNotFoundError: No module named 'scripts.gates'" in rows[0]["reason"]
    assert sys.executable in rows[0]["reason"]
    assert [(op["op"], op["by"], op["source"], op["item"]) for op in world.sent] == [
        ("alert_raise", ME, "setup", "gate-import-reruns")
    ]
    assert "reruns" in world.sent[0]["text"] and sys.executable in world.sent[0]["text"]
    world.now = START + HOUR - 1
    world.run("gh run rerun 7")
    world.now = START + HOUR
    world.run("gh run rerun 7")
    assert len(world.rows()) == 3
    assert world.items() == ["gate-import-reruns", "gate-import-reruns"]


@pytest.mark.feature("gate-import-failure-alerts")
def test_the_identity_pin_shim_is_reported_as_the_identity_gate(world):
    world.place(IDENTITY)
    assert world.run("agentihooks ledger --slug sw get tasks/t2").decision is None
    assert [(row["gate"], row["kind"]) for row in world.rows()] == [("identity", "fail-open")]
    assert world.items() == ["gate-import-identity"]


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_module_launched_gate_names_the_interpreter_from_its_error_line(world, tmp_path):
    world.write("pre-bash-quiet_claim.sh", f"exec {sys.executable} -P -m scripts.gates quiet\n")
    assert world.run("ls").decision is None
    rows = world.rows()
    assert [(row["gate"], row["kind"]) for row in rows] == [("quiet", "fail-open")]
    assert f"import failed under {sys.executable}: No module named scripts.gates" == rows[0]["reason"]
    assert world.items() == ["gate-import-quiet"]


@pytest.mark.feature("gate-import-failure-alerts")
@pytest.mark.parametrize(
    "name, body",
    [
        ("pre-bash-lint_helper.py", "import a_module_that_is_not_installed\n"),
        ("pre-bash-rerun_budget.py", "import sys\nsys.exit('broken differently')\n" + IMPORTED),
        ("pre-bash-mystery.py", 'from scripts.gates.entry import main\n\nraise SystemExit(main(["not-a-gate"]))\n'),
    ],
)
def test_other_condition_failures_keep_the_plain_failed_path(world, name, body):
    world.write(name, body)
    result = world.run("ls")
    assert result.decision is None and "failed" in result.contexts[0]
    assert world.rows() == [] and world.sent == []


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_gate_condition_whose_interpreter_cannot_start_is_not_an_import_failure(world, monkeypatch, tmp_path):
    world.write("pre-bash-rerun_budget.py", IMPORTED)
    monkeypatch.setitem(conditions._RUNNERS, ".py", [str(tmp_path / "no-such-python")])
    result = world.run("ls")
    assert result.decision is None and "failed" in result.contexts[0]
    assert world.rows() == [] and world.sent == []


@pytest.mark.feature("gate-import-failure-alerts")
def test_another_condition_still_denies_beside_a_reported_import_failure(world):
    world.write("pre-bash-rerun_budget.py", IMPORTED)
    world.write("pre-bash-refuse.sh", "echo refused >&2\nexit 2\n")
    result = world.run("ls")
    assert result.decision == "deny" and "refused" in " ".join(result.reasons)
    assert [row["gate"] for row in world.rows()] == ["reruns"]


@pytest.mark.feature("gate-import-failure-alerts")
def test_an_unreachable_ledger_keeps_the_call_allowed_and_the_next_failure_retries(world, monkeypatch):
    world.write("pre-bash-rerun_budget.py", IMPORTED)

    def down(slug: str, path: str, body: dict | None) -> dict:
        raise TimeoutError("ledger writer sent no answer")

    monkeypatch.setattr(
        ledger, "client", lambda slug, timeout=10: ResourceClient("http://ledger.invalid", {}, timeout, down)
    )
    assert world.run("ls").decision is None
    monkeypatch.setattr(
        ledger, "client", lambda slug, timeout=10: ResourceClient("http://ledger.invalid", {}, timeout, world.transport)
    )
    assert world.run("ls").decision is None
    assert len(world.rows()) == 2
    assert world.items() == ["gate-import-reruns"]
