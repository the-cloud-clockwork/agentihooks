"""The expert package role: its render, its two skills, its three conditions and the commands its guidance names."""

import json
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

import hooks.context.context_recycle as recycle
from scripts.inbox.cli import build_parser as msg_parser
from scripts.swarm.cli import build_parser as swarm_parser
from tests.test_package_internal_names import LEAKS
from tests.test_profile_render import world as world

ROLE = Path(__file__).resolve().parents[2] / "profiles" / "package" / "roles" / "expert"
SKILLS = ("swarm-expert", "expert-handoff")
CONDITIONS = [
    "pre-bash.agentihooks+bash.bash+bash.sh+bash.zsh+bash.eval-identity_pin.py",
    "pre-bash.rm+bash.rmdir+bash.bash+bash.sh+bash.zsh+bash.eval-prompt_guard.py",
    "stop-expert_stop.py",
]
OBLIGATION = (
    "You work one mission to its end",
    "You claim no ledger task and pick no other work.",
    "You complete every instruction of the mission. No exception, no workaround, no excuse, no follow up: a problem "
    "you meet is solved at its root, inside the mission.",
    "Your lineage ends only on a mission proof the swarm accepts: every deliverable proven, validated, smoke tested "
    "and regression tested, held by a Semgrep rule and a feature map entry, with nothing left open.",
    "A question goes to the master through the inbox, never to the operator.",
)
LOOP = {
    "swarm-expert": (
        "agentihooks swarm <slug> expert mission",
        "agentihooks swarm <slug> confirm-handoff <transfer> --next",
        "agentihooks swarm <slug> merge queue <url>",
        "agentihooks msg send master@<slug>",
        "agentihooks swarm <slug> wait --on checks <url>",
        "agentihooks swarm <slug> wait --on merge <url>",
        "agentihooks swarm <slug> wait --on reply <id>",
        "agentihooks swarm <slug> wait 30 --reason",
        "agentihooks swarm <slug> learned",
        "agentihooks swarm <slug> handoff <doc>",
        "agentihooks swarm <slug> expert complete <proof>",
        "agentihooks swarm <slug> expert status <lineage>",
        "# Mission proof",
        "<!-- mission complete -->",
    ),
    "expert-handoff": (
        "agentihooks swarm <slug> expert mission",
        "agentihooks swarm <slug> handoff <doc>",
        "agentihooks swarm <slug> confirm-handoff <transfer> --next",
        "# Expert handoff",
        "<!-- handoff complete -->",
    ),
}
PROOF_HEADINGS = (
    "Mission",
    "Deliverables",
    "Validation",
    "Smoke tests",
    "Regression tests",
    "Semgrep rules",
    "Feature map",
    "Open items",
)
HANDOFF_HEADINGS = (
    "Mission",
    "Done",
    "Pending",
    "Assumptions",
    "Tests",
    "Validation",
    "Stopped at",
    "Decisions and promises",
    "Next",
    "Read first",
)
PARSERS = {"swarm": lambda argv: swarm_parser().parse_args(argv), "msg": lambda argv: msg_parser().parse_args(argv)}


def _skill(name: str) -> Path:
    return ROLE / ".claude" / "skills" / name / "SKILL.md"


def _commands(text: str) -> list[str]:
    return [span for span in re.findall(r"`([^`\n]+)`", text) if span.startswith("agentihooks ")]


def _argv(command: str) -> tuple[str, list[str]]:
    tokens = [token.split("|")[0] for token in shlex.split(command)[1:]]
    tokens = ["x" if re.fullmatch(r"<[^>]+>", token) else token for token in tokens]
    if tokens[0] == "swarm" and tokens[1] == "x":
        tokens[1] = "s"
    return tokens[0], tokens[1:]


def _template(name: str) -> list[str]:
    """The headings of the document template the skill ends with."""
    fenced = _skill(name).read_text().rsplit("```markdown\n", 1)[1].split("```")[0]
    return [line[3:] for line in fenced.splitlines() if line.startswith("## ")]


@pytest.mark.feature("expert-role-profile")
def test_the_expert_role_renders_alone_with_its_persona_its_rule_and_both_skills(world: dict[str, Any]) -> None:
    from scripts.profiles import render

    world["install"]._save_state({})
    out = render.render_claude("expert")
    assert out is not None

    persona = (out / "CLAUDE.md").read_text()
    assert (ROLE / "CLAUDE.md").read_text().strip() in persona
    rule = (ROLE / ".claude" / "rules" / "expert-role.md").read_text()
    assert f"<!-- rule: expert-role.md (rule) -->\n{rule.strip()}" in persona
    assert json.loads((out / render.STAMP).read_text())["chain"] == ["expert"]
    assert "mcpServers" not in json.loads((out / "settings.json").read_text())
    for name in SKILLS:
        assert (out / "skills" / name).resolve() == _skill(name).parent
        assert f"{name} skill" in (ROLE / "CLAUDE.md").read_text()
    assert not (ROLE / ".claude" / ".mcp.json").exists() and not (ROLE / ".codex").exists()


@pytest.mark.feature("expert-role-profile")
def test_the_expert_persona_holds_the_operators_obligation() -> None:
    persona = " ".join((ROLE / "CLAUDE.md").read_text().split())

    assert persona.startswith("# Role: Swarm Expert")
    assert [line for line in OBLIGATION if line not in persona] == []


@pytest.mark.feature("expert-role-profile")
def test_the_expert_role_loads_its_identity_pin_its_prompt_guard_and_its_stop_gate(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from hooks.context import conditions

    world["install"]._save_state({})
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "expert")
    layers, _ = conditions.layer_dirs({})
    entries, invalid = conditions.scan_layers(layers)

    assert sorted(entry["file"] for entry in entries if entry["source"] == "profile:expert") == CONDITIONS
    assert invalid == []
    stop = (ROLE / ".claude" / "conditions" / "stop-expert_stop.py").read_text()
    assert stop == 'from scripts.gates.entry import main\n\nraise SystemExit(main(["expert-stop"]))\n'


@pytest.mark.parametrize("name", SKILLS)
def test_each_expert_skill_passes_the_skill_gate(name: str) -> None:
    skill = _skill(name)
    _, front, body = skill.read_text().split("---", 2)
    meta = yaml.safe_load(front)

    assert meta["name"] == skill.parent.name == name
    assert 0 < len(meta["description"]) <= 1024 and not re.search("[<>]", meta["description"])
    assert len(body.splitlines()) < 500
    evals = json.loads((skill.parent / "evals" / "evals.json").read_text())
    assert len(evals) >= 3 and all(entry["query"] and entry["expected_behavior"] for entry in evals)
    leaks = {path.name: LEAKS.findall(path.read_text()) for path in skill.parent.rglob("*") if path.is_file()}
    assert {path: found for path, found in leaks.items() if found} == {}
    assert "~/.claude/skills" not in skill.read_text()


@pytest.mark.feature("expert-role-profile")
@pytest.mark.parametrize("name", SKILLS)
def test_each_expert_skill_covers_its_loop_with_commands_that_parse(name: str) -> None:
    text = _skill(name).read_text()

    assert [step for step in LOOP[name] if step not in text] == []
    commands = _commands(text)
    assert commands
    for command in commands:
        tool, argv = _argv(command)
        assert PARSERS[tool](argv), command


@pytest.mark.feature("expert-role-profile")
def test_the_skill_templates_carry_the_headings_the_swarm_checks() -> None:
    from scripts.handoff import expert

    assert tuple(_template("swarm-expert")) == PROOF_HEADINGS == expert.PROOF.headings
    assert tuple(_template("expert-handoff")) == HANDOFF_HEADINGS == expert.HANDOFF.headings
    assert expert.PROOF.title in _skill("swarm-expert").read_text()
    assert expert.HANDOFF.title in _skill("expert-handoff").read_text()


def test_the_skill_the_context_limit_names_ships_with_the_expert_role() -> None:
    named = re.findall(r"the ([\w-]+) skill", recycle._EXPERT_INSTRUCTIONS)
    assert named == ["expert-handoff"]
    assert all((ROLE / ".claude" / "skills" / name / "SKILL.md").is_file() for name in named)


def test_the_expert_rule_names_agentihooks_commands_that_parse() -> None:
    rule = (ROLE / ".claude" / "rules" / "expert-role.md").read_text()
    spans = re.findall(r"`([^`]+)`", rule)

    assert spans and all(span.startswith("agentihooks ") for span in spans), spans
    for command in spans:
        tool, argv = _argv(command.replace("<minutes>", "30"))
        assert PARSERS[tool](argv), command
