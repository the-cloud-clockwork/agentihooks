import json
import re
import shlex
from pathlib import Path

import pytest
import yaml

from scripts.inbox.cli import build_parser as msg_parser
from scripts.swarm.cli import build_parser as swarm_parser
from scripts.swarm_ledger.ledger import build_parser as ledger_parser
from tests.test_package_internal_names import LEAKS
from tests.test_profile_render import world as render_world

world = render_world
ROLES = Path(__file__).resolve().parents[1] / "profiles" / "package" / "roles"
ROLE_SKILLS = {"master": "swarm-master", "engineer": "swarm-engineer", "cicd": "swarm-ci", "planner": "swarm-planner"}
COMMON = (
    "agentihooks ledger --slug <slug> --as <name> join",
    "agentihooks ledger watch <slug> --as <name>",
    "agentihooks ledger --slug <slug> --as <name> ack",
    "agentihooks msg reply <id>",
    "agentihooks msg close <id>",
    "agentihooks swarm <slug> handoff <doc>",
    "agentihooks swarm <slug> confirm-handoff",
)
WORKER = ("agentihooks swarm <slug> wait --on", "agentihooks swarm <slug> block", "agentihooks swarm <slug> done")
LOOP = {
    "master": (
        "agentihooks swarm <slug> plan approve",
        "agentihooks swarm <slug> plan send-back",
        "agentihooks swarm <slug> verdict",
        "--must",
        "agentihooks swarm <slug> restore-decision",
        "<!-- slice: <id> -->",
        "agentihooks ledger --slug <slug> --as <name> publish-plan",
        "--plan-slice <id>",
    ),
    "engineer": (*WORKER, "agentihooks swarm <slug> issue <url>", "agentihooks swarm <slug> pr <url>", "--pr <url>"),
    "cicd": (*WORKER, "agentihooks swarm <slug> pr <url>", "--pr <url>", "--command", "--output"),
    "planner": (*WORKER, "publish-plan", "--slice <ids>", "--must", "--check", "--judge"),
}
PARSERS = {"swarm": lambda argv: swarm_parser().parse_args(argv), "msg": lambda argv: msg_parser().parse_args(argv)}
PARSERS["ledger"] = lambda argv: ledger_parser().parse_args(argv)


def _skill(role: str) -> Path:
    return ROLES / role / ".claude" / "skills" / ROLE_SKILLS[role] / "SKILL.md"


def test_planner_guidance_nests_task_headings_with_slice_anchors():
    text = " ".join(_skill("planner").read_text().split())

    assert (
        "Put each phase under a heading with its exact title, each task section "
        "under a heading one level deeper, and one unique `<!-- slice: <id> -->` "
        "anchor immediately before each task heading."
    ) in text


@pytest.mark.parametrize(
    "guidance",
    [
        ROLES / "master" / "CLAUDE.md",
        ROLES / "planner" / "CLAUDE.md",
        _skill("master"),
        _skill("planner"),
        ROLES.parent / "skills" / "init-swarm" / "SKILL.md",
        ROLES.parent / "skills" / "take-master" / "SKILL.md",
    ],
)
def test_plan_publishers_distinguish_full_plans_from_standalone_tasks(guidance):
    text = " ".join(guidance.read_text().split())
    rules = (
        "A full plan is a plan file a master or planner writes and publishes "
        "to the artifacts; every task built from it carries its slice.",
        "Follow ups, open questions, operator notes and orders the operator "
        "types or gives are standalone tasks with no plan and no slice.",
        "A standalone task that a master or planner expands because it grew "
        "wide becomes a plan: write and publish the plan with slice markers, "
        "then add its tasks with their slices.",
        "Small self explanatory changes, such as a style tweak or a loose "
        "layout change, stay standalone and never get a plan.",
    )

    assert [rule for rule in rules if rule not in text] == []


def _commands(text: str) -> list[str]:
    return [span for span in re.findall(r"`([^`\n]+)`", text) if span.startswith("agentihooks ")]


def _argv(command: str) -> tuple[str, list[str]]:
    tokens = [t.split("|")[0] for t in shlex.split(command)[1:] if t != "..."]
    tokens = ["x" if re.fullmatch(r"<[^>]+>", t) else t for t in tokens]
    if tokens[0] == "swarm" and tokens[1] == "<slug>":
        tokens[1] = "s"
    return tokens[0], tokens[1:]


@pytest.mark.parametrize("role", ROLE_SKILLS)
def test_role_skill_passes_the_skill_gate(role):
    skill = _skill(role)
    _, front, body = skill.read_text().split("---", 2)
    meta = yaml.safe_load(front)

    assert meta["name"] == skill.parent.name == ROLE_SKILLS[role]
    assert 0 < len(meta["description"]) <= 1024 and not re.search("[<>]", meta["description"])
    assert len(body.splitlines()) < 500
    evals = json.loads((skill.parent / "evals" / "evals.json").read_text())
    assert len(evals) >= 3 and all(e["query"] and e["expected_behavior"] for e in evals)


@pytest.mark.parametrize("role", ROLE_SKILLS)
def test_role_skill_names_nothing_internal(role):
    folder = _skill(role).parent
    leaks = {p.name: LEAKS.findall(p.read_text()) for p in folder.rglob("*") if p.is_file()}

    assert {name: found for name, found in leaks.items() if found} == {}
    assert "~/.claude/skills" not in _skill(role).read_text()


@pytest.mark.parametrize("planted", ["the anton cluster", "see the manifesto", "host 10.0.0.12", "gateway tools"])
def test_leak_check_turns_red_on_a_planted_name(planted):
    assert LEAKS.search(planted)


@pytest.mark.parametrize("role", ROLE_SKILLS)
def test_role_skill_covers_its_loop(role):
    text = _skill(role).read_text()

    assert [step for step in (*COMMON, *LOOP[role]) if step not in text] == []


@pytest.mark.parametrize("role", ROLE_SKILLS)
def test_every_role_skill_command_parses(role):
    commands = [c for c in _commands(_skill(role).read_text()) if not c.startswith("agentihooks ledger watch")]

    assert commands
    for command in commands:
        tool, argv = _argv(command)
        assert PARSERS[tool](argv), command


@pytest.mark.parametrize("role", ROLE_SKILLS)
def test_role_home_lists_its_own_role_skill_only(world, role):
    from scripts.profiles import render

    world["install"]._save_state({})
    out = render.render_claude(role)

    listed = {p.name for p in (out / "skills").iterdir()}
    assert ROLE_SKILLS[role] in listed
    assert listed & set(ROLE_SKILLS.values()) == {ROLE_SKILLS[role]}
    assert (out / "skills" / ROLE_SKILLS[role]).resolve() == _skill(role).parent
    assert f"{ROLE_SKILLS[role]} skill" in (ROLES / role / "CLAUDE.md").read_text()


@pytest.mark.parametrize(
    "skill",
    [
        _skill("engineer"),
        ROLES / "engineer" / ".claude" / "rules" / "engineer-role.md",
        ROLES.parent / "skills" / "worktree" / "SKILL.md",
    ],
)
def test_engineer_guidance_dequeues_a_queued_pull_request_before_pushing_a_fix(skill):
    text = " ".join(skill.read_text().split())
    steps = (
        "only queues",
        "agentihooks swarm <slug> merge queue",
        "refuses a push",
        "dequeue it first",
        "agentihooks swarm <slug> merge dequeue",
        "then push",
        "checks pass",
        "queue it again",
    )

    assert [step for step in steps if step not in text] == []
    assert [text.index(step) for step in steps] == sorted(text.index(step) for step in steps)


@pytest.mark.parametrize("skill", [_skill("engineer"), ROLES / "engineer" / ".claude" / "rules" / "engineer-role.md"])
def test_swarm_engineer_guidance_names_no_raw_gh_merge_or_dequeue_mutation(skill):
    text = skill.read_text()

    assert [raw for raw in ("dequeuePullRequest", "gh pr merge") if raw in text] == []


def test_rendered_qa_persona_reads_tests_from_ci_only(world):
    from scripts.profiles import render

    world["install"]._save_state({})
    persona = " ".join((render.render_claude("qa") / "CLAUDE.md").read_text().split())

    assert "run the gates, mutation and smoke runs" not in persona
    assert "runs the gates" not in (ROLES / "qa" / "profile.yml").read_text()
    steps = ("results from their CI runs", "never on this machine", "AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN")
    assert [step for step in steps if step not in persona] == []


def test_rendered_swarm_engineer_skill_reads_red_and_green_in_ci(world):
    from scripts.profiles import render

    world["install"]._save_state({})
    out = render.render_claude("engineer")
    text = " ".join((out / "skills" / "swarm-engineer" / "SKILL.md").read_text().split())

    assert "lint and tests green" not in text
    steps = (
        "branch mutation preflight",
        "read it red on that branch push's preflight run",
        "wait until it is green on the current head",
        "then open the first draft",
        "read it red in CI on a draft pull request",
        "read it green in CI",
        "AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN",
    )
    assert [step for step in steps if step not in text] == []
    assert [text.index(step) for step in steps] == sorted(text.index(step) for step in steps)


MASTER_POOL = (
    "masters-step",
    "`master@<slug>`",
    "`master-2@<slug>`",
    "first in first out",
    "exactly one",
    "skipping a master that is gone or being retired by the tick",
    "with the same id",
    "Items from the operator bypass the pool and go to the front door",
    "behaves exactly as a single master swarm",
    "agentihooks swarm <slug> masters-channel say",
    "agentihooks swarm <slug> masters-channel read",
    "ten minutes",
    "MASTER_INBOX_CLASSIFIER_WITH_FALLBACKS",
    "Claude and Codex",
)


def _section(text: str, heading: str, until: str = "\n## ") -> str:
    start = text.index(heading)
    end = text.find(until, start + len(heading))
    return text[start : end if end != -1 else len(text)]


def test_rendered_claude_and_codex_masters_carry_the_same_master_pool(world):
    from scripts.profiles import render

    out = render.render_codex("package:master")
    claude = out.parent / "claude"
    pools = {
        home: _section(persona.read_text(), "## Master pool")
        for home, persona in (("claude", claude / "CLAUDE.md"), ("codex", out / "AGENTS.md"))
    }
    skills = {
        home: root / "skills" / "swarm-master" / "SKILL.md" for home, root in (("claude", claude), ("codex", out))
    }

    assert pools["claude"] == pools["codex"]
    assert [phrase for phrase in MASTER_POOL if phrase not in " ".join(pools["claude"].split())] == []
    assert skills["claude"].resolve() == skills["codex"].resolve() == _skill("master")
    skill = " ".join(_section(skills["codex"].read_text(), "## Master pool").split())
    assert [phrase for phrase in MASTER_POOL if phrase not in skill] == []


MASTER_SCALING = (
    "keeps automatic scaling on",
    "agentihooks balance",
    "agentihooks swarm <slug> autoscale",
    "before stating capacity",
    "nothing past host room",
)


def test_rendered_claude_and_codex_masters_carry_the_scaling_rule(world):
    from scripts.profiles import render

    out = render.render_codex("package:master")
    claude = out.parent / "claude"
    rules = {
        home: " ".join(_section(persona.read_text(), "## Scaling").split())
        for home, persona in (("claude", claude / "CLAUDE.md"), ("codex", out / "AGENTS.md"))
    }
    skill = " ".join(_section((claude / "skills" / "swarm-master" / "SKILL.md").read_text(), "## Scaling").split())

    assert rules["claude"] == rules["codex"]
    assert [phrase for phrase in MASTER_SCALING if phrase not in rules["claude"]] == []
    assert [phrase for phrase in MASTER_SCALING if phrase not in skill] == []


def test_swarm_docs_describe_the_master_pool_for_claude_and_codex():
    docs = ROLES.parents[2] / "docs" / "pillars" / "swarm.md"
    text = " ".join(_section(docs.read_text(), "### Master pool", "\n### ").split())
    phrases = [phrase.replace("<slug>", "<id>") for phrase in MASTER_POOL]

    assert [phrase for phrase in phrases if phrase not in text] == []
