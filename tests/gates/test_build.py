"""The build gate: a swarm engineer or CI agent edits and commits only inside its traced plan and task territory."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts.gates import entry
from scripts.gates.base import Call, Gate, Who
from scripts.gates.build import BuildGate, refusal, task_territory
from scripts.gates.verdicts import Verdicts
from scripts.swarm import trace_plan
from scripts.swarm_ledger import ledger_workspace

ME, SLUG, TASK = "engineer@100001-0001", "sw", "t1"
DOGHOUSE = (
    "- walls and roof | doghouse/frame, doghouse/roof | the house needs a shell\n"
    "- a light over the door | doghouse/light | the dog sleeps there at night\n"
    "- a diesel generator | power/generator | it powers the light\n"
)


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    folder = ledger_workspace.folder(SLUG, TASK)
    folder.mkdir(parents=True)
    ledgers = tmp_path / "ledgers"
    ledgers.mkdir()
    gate = BuildGate(environ={"LEDGER_DIR": str(ledgers)})

    def plan(text=DOGHOUSE, verdict="pass", kept=(True, True, False), reasons=(), stale=False):
        (folder / trace_plan.PLAN).write_text(text)
        pieces = trace_plan.parse(text, TASK)
        rows = [
            {"what": p.what, "areas": list(p.areas), "why": p.why, "probability": None, "kept": k}
            for p, k in zip(pieces, kept, strict=False)
        ]
        digest = "stale" if stale else trace_plan.plan_hash(pieces)
        record = {"verdict": verdict, "plan_hash": digest, "pieces": rows, "reasons": list(reasons)}
        (folder / trace_plan.VERDICT).write_text(json.dumps(record))

    def territory(*areas):
        doc = {"tasks": [{"id": TASK, "territory": list(areas)}, {"id": "t2", "territory": ["elsewhere"]}]}
        (ledgers / f"{SLUG}.json").write_text(json.dumps(doc))

    def decide(tool="Edit", name=ME, cwd=None, task=TASK, swarm=SLUG, **tool_input):
        call = Call(tool=tool, tool_input=tool_input, cwd=str(cwd or repo))
        return gate.decide(call, Who(name=name, swarm=swarm, task=task), Verdicts(SLUG, "build", tmp_path / "gates"))

    def rows():
        path = tmp_path / "gates" / SLUG / "gates" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    class World:
        pass

    w = World()
    w.repo, w.folder, w.home, w.plan, w.territory, w.decide, w.rows = (
        repo,
        folder,
        Path.home(),
        plan,
        territory,
        decide,
        rows,
    )
    return w


def edit(world, rel, **extra):
    return world.decide(file_path=str(world.repo / rel), **extra)


class TestMatches:
    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            ("Edit", {"file_path": "/x"}),
            ("Write", {"file_path": "/x"}),
            ("MultiEdit", {"file_path": "/x"}),
            ("NotebookEdit", {"notebook_path": "/x"}),
            ("mcp__serena__replace_symbol_body", {"relative_path": "a.py"}),
            ("mcp__serena__insert_after_symbol", {"relative_path": "a.py"}),
            ("mcp__serena__replace_in_files", {"relative_path": "a.py"}),
            ("Bash", {"command": "git commit -m probe"}),
            ("Bash", {"command": "cd /x && git -C y commit -am probe"}),
        ],
    )
    def test_edits_and_commits_match(self, tool, tool_input):
        assert BuildGate().matches(Call(tool=tool, tool_input=tool_input))

    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            ("Read", {"file_path": "/x"}),
            ("mcp__serena__find_symbol", {"relative_path": "a.py"}),
            ("Bash", {"command": "git status"}),
            ("Bash", {"command": "echo commit"}),
            ("Bash", {"command": "git log --grep commit"}),
        ],
    )
    def test_reads_and_other_commands_do_not(self, tool, tool_input):
        assert not BuildGate().matches(Call(tool=tool, tool_input=tool_input))

    def test_it_is_a_gate_registered_at_the_entry_point_and_observes_first(self):
        assert isinstance(BuildGate(), Gate)
        assert entry.GATES["build"].default_mode == "observe"


class TestWhoIsGated:
    def test_an_unpinned_session_a_master_a_planner_and_a_taskless_agent_pass(self, world):
        assert world.decide(file_path=str(world.repo / "x.py"), swarm="").allowed
        assert world.decide(file_path=str(world.repo / "x.py"), name="master@100001-0001").allowed
        assert world.decide(file_path=str(world.repo / "x.py"), name="planner@100001-0001").allowed
        assert world.decide(file_path=str(world.repo / "x.py"), task="").allowed

    def test_a_ci_agent_is_gated_like_an_engineer(self, world):
        assert not world.decide(file_path=str(world.repo / "x.py"), name="ci@100001-0001").allowed


class TestPlanState:
    def test_an_edit_before_any_plan_is_refused_with_the_trace_command(self, world):
        decision = edit(world, "doghouse/light/lamp.py")
        assert not decision.allowed
        assert str(world.folder / "plan.md") in decision.reason
        assert f"agentihooks swarm {SLUG} trace-plan" in decision.reason

    def test_a_plan_that_does_not_parse_is_refused_with_the_parse_error(self, world):
        (world.folder / "plan.md").write_text("- what | areas\n")
        decision = edit(world, "doghouse/light/lamp.py")
        assert not decision.allowed
        assert "plan line 1 is not a piece" in decision.reason

    def test_a_plan_changed_since_its_verdict_is_refused(self, world):
        world.plan(stale=True)
        decision = edit(world, "doghouse/light/lamp.py")
        assert not decision.allowed
        assert "no verdict for its current text" in decision.reason
        assert f"agentihooks swarm {SLUG} trace-plan" in decision.reason

    def test_a_failed_plan_is_refused_with_its_reasons(self, world):
        world.plan(verdict="fail", reasons=["2 of 3 pieces are off the task intent, more than half"])
        decision = edit(world, "doghouse/light/lamp.py")
        assert not decision.allowed
        assert "2 of 3 pieces are off the task intent" in decision.reason

    def test_an_unchecked_plan_lets_every_edit_through_counted(self, world):
        world.plan(verdict="unchecked", kept=(True, True, True))
        assert edit(world, "anywhere/else.py").allowed
        assert [(r["gate"], r["kind"], r["agent"], r["task"]) for r in world.rows()] == [("build", "count", ME, TASK)]


class TestAreas:
    def test_the_generator_area_is_refused_and_the_light_area_allowed(self, world):
        world.plan()
        assert edit(world, "doghouse/light/lamp.py").allowed
        assert edit(world, "doghouse/frame").allowed
        denied = edit(world, "power/generator/diesel.py")
        assert not denied.allowed
        assert "power/generator/diesel.py" in denied.reason
        assert "doghouse/frame, doghouse/roof, doghouse/light" in denied.reason

    def test_a_sibling_whose_name_extends_an_area_is_outside_it(self, world):
        world.plan()
        assert not edit(world, "doghouse/lightning.py").allowed

    def test_the_task_territory_widens_the_kept_areas(self, world):
        world.plan()
        world.territory("./power/", "docs")
        assert edit(world, "power/generator/diesel.py").allowed
        assert edit(world, "docs/x.md").allowed
        assert not edit(world, "elsewhere/x.py").allowed

    def test_the_work_folder_the_scratchpad_and_files_outside_git_are_exempt(self, world):
        assert world.decide(file_path=str(world.folder / "plan.md")).allowed
        assert world.decide(file_path=str(world.home / "scratchpad" / "me" / "notes.md")).allowed
        assert world.decide(file_path=str(world.home / "loose" / "file.py")).allowed

    def test_a_relative_path_resolves_against_the_call_directory(self, world):
        world.plan()
        assert world.decide(file_path="lamp.py", cwd=world.repo / "doghouse" / "light").allowed
        assert not world.decide(file_path="../../power/x.py", cwd=world.repo / "doghouse" / "light").allowed

    def test_a_notebook_edit_reads_its_notebook_path(self, world):
        world.plan()
        assert not world.decide(tool="NotebookEdit", notebook_path=str(world.repo / "power" / "n.ipynb")).allowed

    def test_serena_relative_paths_are_repo_relative(self, world):
        world.plan()
        assert world.decide(tool="mcp__serena__replace_symbol_body", relative_path="doghouse/light/lamp.py").allowed
        assert not world.decide(tool="mcp__serena__replace_content", relative_path="./power/x.py").allowed
        assert not world.decide(tool="mcp__serena__replace_in_files", relative_path="").allowed
        assert world.decide(tool="mcp__serena__replace_in_files", relative_path="", dry_run=True).allowed

    def test_a_codex_patch_checks_every_file_it_names(self, world):
        world.plan()
        patch = (
            "*** Begin Patch\n*** Update File: doghouse/light/lamp.py\n@@\n-a\n+b\n"
            "*** Add File: power/generator/diesel.py\n+x\n*** End Patch\n"
        )
        denied = world.decide(file_path="doghouse/light/lamp.py", content=patch)
        assert not denied.allowed
        assert "power/generator/diesel.py" in denied.reason
        assert world.decide(file_path="doghouse/light/lamp.py", content=patch.split("*** Add")[0]).allowed


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def staged(world):
    import shutil

    shutil.rmtree(world.repo / ".git")
    git(world.repo, "init", "-q")
    git(world.repo, "config", "user.email", "t@t")
    git(world.repo, "config", "user.name", "t")
    for rel in ("doghouse/light/lamp.py", "power/generator/diesel.py"):
        (world.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (world.repo / rel).write_text("a\n")
    git(world.repo, "add", "doghouse/light/lamp.py", "power/generator/diesel.py")
    git(world.repo, "commit", "-qm", "base")
    world.plan()
    return world


class TestCommit:
    def test_a_commit_of_staged_files_inside_the_areas_passes(self, staged):
        (staged.repo / "doghouse/light/lamp.py").write_text("b\n")
        git(staged.repo, "add", "doghouse/light/lamp.py")
        assert staged.decide(tool="Bash", command='git commit -m "light it"').allowed

    def test_a_staged_file_outside_the_areas_is_refused(self, staged):
        (staged.repo / "power/generator/diesel.py").write_text("b\n")
        git(staged.repo, "add", "power/generator/diesel.py")
        denied = staged.decide(tool="Bash", command="git commit -m fuel")
        assert not denied.allowed
        assert "power/generator/diesel.py" in denied.reason

    def test_commit_all_counts_tracked_changes_that_are_not_staged(self, staged):
        (staged.repo / "power/generator/diesel.py").write_text("b\n")
        assert staged.decide(tool="Bash", command="git commit -m fuel").allowed
        assert not staged.decide(tool="Bash", command="git commit -am fuel").allowed
        assert not staged.decide(tool="Bash", command="git commit --all -m fuel").allowed

    def test_the_commit_directory_follows_cd_and_dash_c(self, staged):
        (staged.repo / "power/generator/diesel.py").write_text("b\n")
        git(staged.repo, "add", "power/generator/diesel.py")
        elsewhere = staged.home
        assert not staged.decide(tool="Bash", command=f"cd {staged.repo} && git commit -m x", cwd=elsewhere).allowed
        assert not staged.decide(tool="Bash", command=f"git -C {staged.repo} commit -m x", cwd=elsewhere).allowed
        assert staged.decide(tool="Bash", command="git commit -m x", cwd=elsewhere).allowed


class TestTerritory:
    def test_a_missing_ledger_or_task_reads_as_no_territory(self, tmp_path):
        assert task_territory(tmp_path, SLUG, TASK) == []
        (tmp_path / f"{SLUG}.json").write_text("{not json")
        assert task_territory(tmp_path, SLUG, TASK) == []
        (tmp_path / f"{SLUG}.json").write_text(json.dumps({"tasks": [{"id": "t2", "territory": ["a"]}]}))
        assert task_territory(tmp_path, SLUG, TASK) == []


def test_the_refusal_names_the_files_the_areas_and_both_ways_forward():
    who = Who(name=ME, swarm=SLUG, task=TASK)
    text = refusal(who, [f"f{i}.py" for i in range(7)], ["a", "b"], "/w/plan.md")
    assert "f0.py, f1.py, f2.py, f3.py, f4.py and 2 more" in text
    assert "Kept areas and territory: a, b" in text
    assert f"agentihooks swarm {SLUG} trace-plan" in text
    assert f"agentihooks ledger --slug {SLUG} --as {ME} followup add" in text
