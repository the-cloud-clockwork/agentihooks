"""A gate condition whose child cannot import the gates package is reported by the healthy hook process that ran it:
the call goes through, a fail-open row names the gate and the import error, and one setup alert per gate per hour
names the gate and the interpreter."""

import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from hooks.context import conditions, gate_import, profile_chain
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
SHIM_LINE = "    from scripts.gates.entry import main"
LOST = "a_dependency_lost_with_the_worktree"
RUNPY = '  File "<frozen runpy>", line 112, in _get_module_details'
CHILD = """import json
from importlib import import_module

from scripts.gates import entry
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api.client import ResourceClient


def transport(slug, path, body):
    if body is not None:
        with open({sent!r}, "a") as out:
            out.write(json.dumps(body["ops"][0]) + "\\n")
    return {{"revision": 1}}


ledger.client = lambda slug, timeout=10: ResourceClient("http://ledger.invalid", {{}}, timeout, transport)
entry.GATES["absent"] = (lambda: import_module("scripts.gates.absent_gate_module"),)
raise SystemExit(entry.main(["absent"]))
"""


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
        result = conditions.run_step("pre", payload)
        assert result is not None
        return result

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
    (broken / "gates").mkdir(parents=True)
    (broken / "__init__.py").write_text("")
    (broken / "gates" / "__init__.py").write_text(f"import {LOST}\n")
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
def test_a_rerun_budget_shim_that_cannot_import_the_gates_is_reported_once_per_hour(world: World) -> None:
    world.place(RERUNS)
    result = world.run("gh run rerun 7")
    assert result.decision is None and result.input_patch == {}
    assert "failed" in result.contexts[0]
    rows = world.rows()
    assert [(row["gate"], row["kind"], row["agent"], row["task"], row["tool"]) for row in rows] == [
        ("reruns", "fail-open", ME, TASK, "Bash")
    ]
    assert f"ModuleNotFoundError: No module named '{LOST}'" in rows[0]["reason"]
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
def test_the_identity_pin_shim_is_reported_as_the_identity_gate(world: World) -> None:
    world.place(IDENTITY)
    assert world.run("agentihooks ledger --slug sw get tasks/t2").decision is None
    assert [(row["gate"], row["kind"]) for row in world.rows()] == [("identity", "fail-open")]
    assert world.items() == ["gate-import-identity"]


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_module_launched_gate_is_reported_from_its_launch_traceback(world: World, tmp_path: Path) -> None:
    world.write("pre-bash-quiet_claim.sh", f"exec {sys.executable} -P -m scripts.gates quiet\n")
    result = world.run("ls")
    assert result.decision is None and LOST in " ".join(result.contexts)
    rows = world.rows()
    assert [(row["gate"], row["kind"]) for row in rows] == [("quiet", "fail-open")]
    assert (
        rows[0]["reason"]
        == f"import failed under an unknown interpreter: ModuleNotFoundError: No module named '{LOST}'"
    )
    assert world.items() == ["gate-import-quiet"]


@pytest.mark.feature("gate-import-failure-alerts")
@pytest.mark.parametrize(
    "name, body",
    [
        ("pre-bash-lint_helper.py", "import a_module_that_is_not_installed\n"),
        ("pre-bash-rerun_budget.py", "import a_module_that_is_not_installed\n" + IMPORTED),
        ("pre-bash-rerun_budget.py", "import sys\nsys.exit('broken differently')\n" + IMPORTED),
        ("pre-bash-mystery.py", 'from scripts.gates.entry import main\n\nraise SystemExit(main(["not-a-gate"]))\n'),
    ],
)
def test_other_condition_failures_keep_the_plain_failed_path(world: World, name: str, body: str) -> None:
    world.write(name, body)
    result = world.run("ls")
    assert result.decision is None and "failed" in result.contexts[0]
    assert world.rows() == [] and world.sent == []


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_gate_condition_whose_interpreter_cannot_start_is_not_an_import_failure(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world.write("pre-bash-rerun_budget.py", IMPORTED)
    monkeypatch.setitem(conditions._RUNNERS, ".py", [str(tmp_path / "no-such-python")])
    result = world.run("ls")
    assert result.decision is None and "failed" in result.contexts[0]
    assert world.rows() == [] and world.sent == []


@pytest.mark.feature("gate-import-failure-alerts")
def test_another_condition_still_denies_beside_a_reported_import_failure(world: World) -> None:
    world.write("pre-bash-rerun_budget.py", IMPORTED)
    world.write("pre-bash-refuse.sh", "echo refused >&2\nexit 2\n")
    result = world.run("ls")
    assert result.decision == "deny" and "refused" in " ".join(result.reasons)
    assert [row["gate"] for row in world.rows()] == ["reruns"]


@pytest.mark.feature("gate-import-failure-alerts")
def test_an_unreachable_ledger_keeps_the_call_allowed_and_the_next_failure_retries(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
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


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_host_gate_shim_is_reported_under_its_host_gate_name(world: World) -> None:
    world.write(
        "pre-bash-prompt_guard.py", 'from scripts.gates.entry import main\n\nraise SystemExit(main(["prompts"]))\n'
    )
    assert world.run("ls").decision is None
    assert [(row["gate"], row["kind"]) for row in world.rows()] == [("prompts", "fail-open")]


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_parent_that_cannot_load_the_reporter_names_the_miss_and_keeps_the_call(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.gates

    monkeypatch.delattr(scripts.gates, "import_failure", raising=False)
    monkeypatch.setitem(sys.modules, "scripts.gates.import_failure", None)
    world.write("pre-bash-rerun_budget.py", IMPORTED)
    assert world.run("ls").decision is None
    assert world.rows() == [] and world.sent == []
    condition = world.directory / "pre-bash-rerun_budget.py"
    assert f"agentihooks gate reruns: import failure of {condition} not reported: " in capsys.readouterr().err


@pytest.mark.feature("gate-import-failure-alerts")
@pytest.mark.parametrize(
    "body, gate",
    [
        (IMPORTED, "reruns"),
        ("exec python -P -m scripts.gates quiet\n", "quiet"),
        (IMPORTED + 'main(["identity"])\n', ""),
        ("print('no gate here')\n", ""),
    ],
)
def test_a_condition_names_its_gate_only_when_it_runs_exactly_one(tmp_path: Path, body: str, gate: str) -> None:
    path = tmp_path / "condition"
    path.write_text(body)
    assert gate_import.gate_name(path) == gate


@pytest.mark.feature("gate-import-failure-alerts")
def test_an_unreadable_condition_names_no_gate(tmp_path: Path) -> None:
    assert gate_import.gate_name(tmp_path / "missing.py") == ""


@pytest.mark.feature("gate-import-failure-alerts")
@pytest.mark.parametrize(
    "stderr, ext, found",
    [
        (f"Traceback\n  File x\n{SHIM_LINE}\nModuleNotFoundError: No module named 'scripts'\n\n", ".py", "runner"),
        (f"Traceback\n{SHIM_LINE}\nImportError: cannot import name 'main'", ".sh", "an unknown interpreter"),
        ("/opt/python/bin/python3: No module named scripts.gates\n", ".sh", "/opt/python/bin/python3"),
        (f"Traceback\n{RUNPY}\nModuleNotFoundError: No module named '{LOST}'\n", ".sh", "an unknown interpreter"),
        ("Traceback\n  File x, line 198, in _run_code\nModuleNotFoundError: No module named 'hooks.x'\n", ".sh", None),
        (f"Traceback\n{SHIM_LINE}\nModuleNotFoundError: No module named 'scripts'\nRuntimeError: later\n", ".py", None),
        (
            "Traceback\n    raise SystemExit(main(['brain']))\nModuleNotFoundError: No module named 'hooks.x'\n",
            ".py",
            None,
        ),
        ("Traceback\n    import requests\nModuleNotFoundError: No module named 'requests'\n", ".py", None),
        ("/opt/python/bin/python3: No module named requests\n", ".sh", None),
        ("", ".py", None),
    ],
)
def test_only_a_child_that_died_importing_the_gates_counts(stderr: str, ext: str, found: str | None) -> None:
    failed = gate_import.import_failure(stderr, ext)
    assert (failed[1] if failed else None) == (sys.executable if found == "runner" else found)


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_failure_the_child_reports_is_not_reported_again_by_the_hook(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    sent = tmp_path / "child-sent.jsonl"
    world.write("pre-bash-absent_gate.py", CHILD.format(sent=str(sent)))
    result = world.run("ls")
    assert result.decision is None and result.contexts == []
    assert [(row["gate"], row["kind"]) for row in world.rows()] == [("absent", "fail-open")]
    assert [json.loads(line)["item"] for line in sent.read_text().splitlines()] == ["gate-import-absent"]
    assert world.sent == []
