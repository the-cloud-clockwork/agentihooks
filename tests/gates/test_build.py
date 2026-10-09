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

    def test_the_shared_clearance_file_is_always_allowed_and_nothing_beside_it(self, world):
        world.plan()
        assert edit(world, "mutation-cleared.txt").allowed
        assert edit(world, "mutation-clearances/ruling.json").allowed
        assert not edit(world, "mutation-clearances.bak/ruling.json").allowed
        assert not edit(world, "scripts/mutation-clearances/ruling.json").allowed
        assert not edit(world, "mutation-cleared.txt.bak").allowed
        assert not edit(world, "scripts/mutation-cleared.txt").allowed
        assert not edit(world, "power/generator/diesel.py").allowed

    def test_a_kept_clearance_piece_passes_the_gate_and_a_cut_area_is_refused(self, world):
        plan = DOGHOUSE + "- append the rulings | mutation-cleared.txt | the mutation gate reads them\n"
        world.plan(plan, kept=(True, True, False, True))
        assert edit(world, "mutation-cleared.txt").allowed
        assert not edit(world, "power/generator/diesel.py").allowed

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
        (tmp_path / f"{SLUG}.json").write_text("{}")
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


def test_the_refusal_lists_up_to_five_files_whole_and_says_none_for_no_areas():
    who = Who(name=ME, swarm=SLUG, task=TASK)
    text = refusal(who, [f"f{i}.py" for i in range(5)], [], "/w/plan.md")
    assert "outside your traced plan: f0.py, f1.py, f2.py, f3.py, f4.py. Kept areas and territory: none. " in text
    assert "append a piece to /w/plan.md, one piece per line: - what | area, area | why, and run" in text


@pytest.mark.parametrize(
    ("command", "found"),
    [
        ("echo commit; git -C", False),
        ("echo commit; git -c", False),
        ("A=1; git commit -m x", True),
        ("git -c a=b commit", True),
    ],
)
def test_trailing_options_and_assignments_parse(command, found):
    assert BuildGate().matches(Call(tool="Bash", tool_input={"command": command})) == found


class TestMore:
    def test_failed_reasons_are_joined_with_and(self, world):
        world.plan(verdict="fail", reasons=["one reason", "another reason"])
        assert "the plan failed its trace: one reason and another reason. Revise plan.md" in edit(world, "a.py").reason

    def test_the_plan_parses_with_the_task_id_like_trace_plan(self, world):
        task = "abc123def456abcd"
        folder = ledger_workspace.folder(SLUG, task)
        folder.mkdir(parents=True)
        (folder / "plan.md").write_text(DOGHOUSE)
        (world.repo.parent / "ledgers" / f"{SLUG}.json").write_text("{}")
        assert "commit hash" in world.decide(file_path=str(world.repo / "a.py"), task=task).reason

    def test_registered_doctor_task_plan_allows_editing_kept_area(self, world):
        task = "fx-8be892c4-code"
        world.plan()
        folder = ledger_workspace.folder(SLUG, task)
        folder.mkdir(parents=True)
        (folder / trace_plan.PLAN).write_text(DOGHOUSE)
        (folder / trace_plan.VERDICT).write_text((world.folder / trace_plan.VERDICT).read_text())
        ledgers = world.repo.parent / "ledgers"
        (ledgers / f"{SLUG}.json").write_text(json.dumps({"tasks": [{"id": task}]}))
        result = world.decide(file_path=str(world.repo / "doghouse/frame/a.py"), task=task)
        assert result.allowed, result.reason

    def test_the_unchecked_count_names_the_tool_and_the_paths(self, world):
        world.plan(verdict="unchecked", kept=(True, True, True))
        patch = "*** Begin Patch\n*** Add File: a.py\n+x\n*** Add File: b.py\n+x\n*** End Patch\n"
        assert world.decide(content=patch).allowed
        row = world.rows()[0]
        assert (row["tool"], row["reason"]) == ("Edit", "unchecked plan, edit allowed: a.py, b.py")

    def test_the_generator_refusal_names_the_plan_file(self, world):
        world.plan()
        assert str(world.folder / "plan.md") in edit(world, "power/generator/diesel.py").reason

    def test_an_area_keeps_its_letters_when_its_slash_is_trimmed(self, world):
        world.plan()
        world.territory("tools/MAX/")
        assert edit(world, "tools/MAX/a.py").allowed

    def test_the_territory_defaults_to_the_development_ledger_folder(self, world):
        world.plan()
        ledgers = Path.home() / "development-ledger"
        ledgers.mkdir()
        (ledgers / f"{SLUG}.json").write_text(json.dumps({"tasks": [{"id": TASK, "territory": ["power"]}]}))
        call = Call(tool="Edit", tool_input={"file_path": str(world.repo / "power/x.py")}, cwd=str(world.repo))
        decision = BuildGate(environ={}).decide(call, Who(name=ME, swarm=SLUG, task=TASK), None)
        assert decision.allowed

    def test_the_work_folder_and_scratchpad_are_exempt_inside_a_git_tree(self, world):
        (world.home / ".git").mkdir()
        assert world.decide(file_path=str(world.folder / "notes.md")).allowed
        assert world.decide(file_path=str(world.home / "scratchpad" / "me" / "notes.md")).allowed
        assert not world.decide(file_path=str(world.home / "elsewhere" / "notes.md")).allowed

    def test_a_patch_checks_files_after_an_exempt_one(self, world):
        world.plan()
        patch = (
            f"*** Begin Patch\n*** Add File: {world.home / 'scratchpad' / 'x.md'}\n+x\n"
            "*** Add File: power/generator/diesel.py\n+x\n*** End Patch\n"
        )
        assert not world.decide(content=patch).allowed

    def test_an_edit_naming_no_file_passes(self, world):
        assert world.decide(content="x").allowed


class TestCommitParsing:
    @pytest.mark.parametrize(
        "command",
        [
            "git -c user.name=x commit -m y",
            "git --no-pager commit -m y",
            "echo hi && git commit -m y",
            "A=1; git commit -m y",
            "git add doghouse/light/lamp.py && git commit -m y",
        ],
    )
    def test_the_commit_is_found_past_options_and_other_commands(self, staged, command):
        (staged.repo / "power/generator/diesel.py").write_text("b\n")
        git(staged.repo, "add", "power/generator/diesel.py")
        assert not staged.decide(tool="Bash", command=command).allowed

    def test_a_later_commit_in_the_same_command_is_checked(self, staged):
        (staged.repo / "power/generator/diesel.py").write_text("b\n")
        git(staged.repo, "add", "power/generator/diesel.py")
        command = f"git -C {staged.home} commit -m a; git commit -m b"
        assert not staged.decide(tool="Bash", command=command).allowed

    def test_every_staged_name_is_listed_whole(self, staged):
        for rel in ("power/generator/big tank.py", "power/x.py"):
            (staged.repo / rel).write_text("b\n")
            git(staged.repo, "add", rel)
        reason = staged.decide(tool="Bash", command="git commit -m y").reason
        assert "outside your traced plan: power/generator/big tank.py, power/x.py." in reason

    def test_a_bare_cd_and_a_tilde_go_home(self, staged):
        home = staged.home
        git(home, "init", "-q")
        (home / "power").mkdir()
        (home / "power" / "a.py").write_text("a\n")
        git(home, "add", "power/a.py")
        assert not staged.decide(tool="Bash", command="cd && git commit -m y").allowed
        assert not staged.decide(tool="Bash", command="cd ~ && git commit -m y").allowed
        assert staged.decide(tool="Bash", command="git commit -m y").allowed
