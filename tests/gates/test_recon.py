"""The recon gate: a swarm engineer or CI agent writes recon.md in its task work folder before its first code edit."""

import hashlib
import io
import json
from pathlib import Path

import pytest

from scripts.gates import entry, modes
from scripts.gates.base import Call, Decision, Gate, Who
from scripts.gates.build import BuildGate
from scripts.gates.recon import HEADINGS, MAX_LINES, RECON, ReconGate, named, problem, refusal, sections, valid
from scripts.gates.verdicts import Verdicts
from scripts.swarm_ledger import ledger_workspace

ME, SLUG, TASK = "engineer@100001-0001", "sw", "t1"


def recon_text(*headings: str, lines: int = 1) -> str:
    return "".join(f"{heading}\n" + "a line of content\n" * lines for heading in headings)


VALID = recon_text(*HEADINGS)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.folder = ledger_workspace.folder(SLUG, TASK)
        self.folder.mkdir(parents=True)
        self.gates = tmp_path / "gates"
        self.gate = ReconGate()

    @property
    def path(self) -> Path:
        return self.folder / RECON

    def write(self, text: str) -> None:
        self.path.write_text(text)

    def decide(self, name: str = ME, task: str = TASK, swarm: str = SLUG, file_path: str = "") -> Decision:
        call = Call(tool="Edit", tool_input={"file_path": file_path or str(self.repo / "app.py")}, cwd=str(self.repo))
        return self.gate.decide(call, Who(name=name, swarm=swarm, task=task), Verdicts(SLUG, "recon", self.gates))

    def rows(self) -> list[dict]:
        path = self.gates / SLUG / "gates" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.mark.feature("recon-gate")
def test_it_is_a_gate_named_recon_that_ships_log_only_and_judges_the_build_gates_calls():
    gate = ReconGate()
    calls = [
        Call("Edit"),
        Call("Write"),
        Call("MultiEdit"),
        Call("NotebookEdit"),
        Call("mcp__serena__replace_symbol_body"),
        Call("mcp__serena__find_symbol"),
        Call("Bash", {"command": "ls"}),
        Call("Read"),
    ]
    assert isinstance(gate, Gate)
    assert (gate.name, gate.default_mode) == ("recon", "observe")
    assert isinstance(entry.GATES["recon"], ReconGate)
    assert [gate.matches(call) for call in calls] == [True, True, True, True, True, False, False, False]
    assert [gate.matches(call) for call in calls] == [BuildGate().matches(call) for call in calls]


@pytest.mark.feature("recon-gate")
def test_a_missing_recon_plan_refuses_the_first_code_edit_naming_the_file_and_the_headings(world: World):
    decision = world.decide()
    assert decision.allowed is False
    assert decision.reason == (
        f"recon gate: {world.path}: no recon plan yet. Before the first code edit write it with these headings in "
        "this order, each followed by its content, at most 500 lines: ## What will be built, ## Code areas touched, "
        "## What might break, ## Regression tests, ## Resources needed, ## Protection plan"
    )
    assert world.rows() == []


@pytest.mark.feature("recon-gate")
def test_a_valid_recon_plan_lets_the_edit_through_and_logs_one_pass_per_text(world: World):
    world.write(VALID)
    assert world.decide().allowed
    assert world.decide().allowed
    [row] = world.rows()
    assert {key: row[key] for key in ("gate", "kind", "agent", "task", "tool", "reason")} == {
        "gate": "recon",
        "kind": "count",
        "agent": ME,
        "task": TASK,
        "tool": "Edit",
        "reason": f"valid recon plan: {world.path}",
    }
    held = Verdicts(SLUG, "recon", world.gates).read(TASK) or {}
    assert (held["verdict"], held["reason"], held["digest"]) == (
        "pass",
        f"valid recon plan: {world.path}",
        hashlib.sha256(VALID.encode()).hexdigest(),
    )
    world.write(VALID + "a later line\n")
    assert world.decide().allowed
    assert [row["kind"] for row in world.rows()] == ["count", "count"]


@pytest.mark.feature("recon-gate")
@pytest.mark.parametrize("missing", HEADINGS)
def test_each_missing_heading_is_named_in_the_refusal(world: World, missing: str):
    world.write(recon_text(*(heading for heading in HEADINGS if heading != missing)))
    decision = world.decide()
    assert decision.allowed is False
    assert decision.reason == refusal(world.path, f'the heading "{missing}" is missing')
    assert world.rows() == []


@pytest.mark.feature("recon-gate")
def test_an_empty_heading_is_named_in_the_refusal(world: World):
    text = VALID.replace("## What might break\na line of content\n", "## What might break\n\n   \n")
    world.write(text)
    decision = world.decide()
    assert decision.allowed is False
    assert decision.reason == refusal(world.path, 'the heading "## What might break" has no content')


@pytest.mark.feature("recon-gate")
def test_headings_out_of_order_or_repeated_are_refused():
    swapped = recon_text(HEADINGS[1], HEADINGS[0], *HEADINGS[2:])
    repeated = VALID + recon_text(HEADINGS[0])
    expected = (
        "the headings are out of order or repeated; write each once, in this order: ## What will be built, "
        "## Code areas touched, ## What might break, ## Regression tests, ## Resources needed, ## Protection plan"
    )
    assert problem(swapped) == expected
    assert problem(repeated) == expected


@pytest.mark.feature("recon-gate")
def test_more_than_five_hundred_lines_is_refused_and_five_hundred_pass(world: World):
    assert MAX_LINES == 500
    filler = "x\n" * (MAX_LINES - len(VALID.splitlines()))
    assert problem(VALID + filler) == ""
    world.write(VALID + filler + "x\n")
    decision = world.decide()
    assert decision.allowed is False
    assert decision.reason == refusal(world.path, "it holds 501 lines, more than 500")


@pytest.mark.feature("recon-gate")
def test_deeper_headings_and_stripped_heading_lines_count_as_the_plan():
    text = "# Recon for the task\nintro\n" + VALID.replace(
        "## What might break\na line of content\n", "## What might break  \n### Detail\n"
    )
    assert problem(text) == ""
    assert problem(VALID.replace("a line of content\n", "#hashtag\n", 1)) == ""
    assert problem(VALID.replace("## Protection plan\na line of content\n", "## Protection plan\n# Next\nx\n")) == (
        'the heading "## Protection plan" has no content'
    )


@pytest.mark.feature("recon-gate")
def test_hash_lines_inside_a_fenced_block_are_content_not_headings():
    sample = "```bash\n# a shell comment\n## not a heading\n```\n"
    text = VALID.replace(f"{HEADINGS[1]}\na line of content\n", f"{HEADINGS[1]}\n{sample}")
    assert problem(text) == ""
    assert sections(text)[1] == (HEADINGS[1], sample.splitlines())
    indented = "  ```\n# a comment at the margin\n  ```\n"
    text = VALID.replace(f"{HEADINGS[1]}\na line of content\n", f"{HEADINGS[1]}\n{indented}")
    assert sections(text)[1] == (HEADINGS[1], indented.splitlines())
    assert problem(VALID.replace(f"{HEADINGS[1]}\na line of content\n", f"{HEADINGS[1]}\n# a shell comment\n")) == (
        'the heading "## Code areas touched" has no content'
    )


@pytest.mark.feature("recon-gate")
def test_an_edit_inside_the_work_folder_the_scratchpad_or_outside_git_always_passes(world: World):
    assert world.decide(file_path=str(Path.home() / "loose" / "notes.md")).allowed
    (Path.home() / ".git").mkdir()
    assert world.decide(file_path=str(world.path)).allowed
    assert world.decide(file_path=str(Path.home() / "scratchpad" / "me" / "notes.md")).allowed
    assert not world.decide(file_path=str(Path.home() / "elsewhere" / "notes.md")).allowed
    assert world.rows() == []


@pytest.mark.feature("recon-gate")
def test_only_pinned_eng_and_ci_agents_on_a_task_are_judged(world: World):
    assert world.decide(task="").allowed
    assert world.decide(swarm="").allowed
    assert world.decide(name="").allowed
    assert world.decide(name="master@100001-0001").allowed
    assert world.decide(name="planner@100001-0001").allowed
    assert not world.decide(name="ci@100001-0001").allowed


@pytest.mark.feature("recon-gate")
def test_sections_valid_and_named_read_the_plan_from_the_work_folder(world: World):
    assert sections("lead\n## A\nx\n# B\n## C\n") == [("## A", ["x"]), ("# B", []), ("## C", [])]
    assert (valid(world.folder), named(world.folder)) == (False, "")
    world.write(VALID.replace(f"{HEADINGS[1]}\na line", f"{HEADINGS[1]}\nscripts/gates/recon.py\na line", 1))
    assert valid(world.folder)
    assert named(world.folder) == "scripts/gates/recon.py\na line of content\na line of content"


def run(world: World, monkeypatch: pytest.MonkeyPatch, names: list[str], gates: dict[str, str]) -> tuple[int, str]:
    monkeypatch.setattr(modes, "swarm_gates", lambda swarm, environ: gates)
    monkeypatch.setenv("LEDGER_DIR", str(world.gates.parent / "ledgers"))
    (world.gates.parent / "ledgers").mkdir(exist_ok=True)
    environ = {"AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": ME, "AGENTIHOOKS_SWARM_TASK": TASK}
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(world.repo / "app.py")}, "cwd": str(world.repo)}
    err = io.StringIO()
    monkeypatch.setattr("sys.stderr", err)
    code = entry.main(names, io.StringIO(json.dumps(payload)), environ, world.gates)
    return code, err.getvalue()


@pytest.mark.feature("recon-gate")
def test_the_build_gate_entry_judges_recon_first_and_stops_at_its_deny(world: World, monkeypatch: pytest.MonkeyPatch):
    code, err = run(world, monkeypatch, ["build"], {"recon": "enforce", "build": "enforce"})
    assert (code, err) == (2, refusal(world.path, "no recon plan yet") + "\n")
    assert [(row["gate"], row["kind"]) for row in world.rows()] == [("recon", "deny")]


@pytest.mark.feature("recon-gate")
def test_the_build_gate_still_judges_the_same_call_after_recon_passes(world: World, monkeypatch: pytest.MonkeyPatch):
    world.write(VALID)
    code, err = run(world, monkeypatch, ["build"], {"recon": "enforce", "build": "enforce"})
    assert code == 2
    assert err.startswith("build gate: no traced plan yet")
    assert [(row["gate"], row["kind"]) for row in world.rows()] == [("recon", "count"), ("build", "deny")]


@pytest.mark.feature("recon-gate")
def test_recon_at_log_only_logs_the_would_be_deny_and_lets_the_build_gate_decide(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    code, _ = run(world, monkeypatch, ["build"], {"build": "off"})
    assert code == 0
    [row] = world.rows()
    assert (row["gate"], row["kind"], row["reason"]) == ("recon", "observe", refusal(world.path, "no recon plan yet"))


@pytest.mark.feature("recon-gate")
def test_the_recon_gate_runs_alone_by_name_and_unknown_or_missing_names_exit_one(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    assert run(world, monkeypatch, ["recon"], {"recon": "enforce", "build": "enforce"})[0] == 2
    assert [(row["gate"], row["kind"]) for row in world.rows()] == [("recon", "deny")]
    assert run(world, monkeypatch, [], {})[0] == 1
    assert run(world, monkeypatch, ["recon", "nope"], {"recon": "enforce"})[0] == 1
    assert len(world.rows()) == 1
    assert entry.BEFORE == {"build": ("recon",)}
