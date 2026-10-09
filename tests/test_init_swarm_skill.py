import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

SKILL = Path(__file__).parent.parent / "profiles/package/skills/init-swarm/SKILL.md"


def _help(*argv: str) -> str:
    done = subprocess.run(["agentihooks", *argv, "--help"], capture_output=True, text=True, check=True)
    return done.stdout


def test_skill_has_frontmatter():
    head = SKILL.read_text().split("---")[1]
    assert re.search(r"^name: init-swarm$", head, re.M)
    assert re.search(r"^description:", head, re.M)


def test_skill_passes_the_skill_gate():
    _, front, body = SKILL.read_text().split("---", 2)
    meta = yaml.safe_load(front)
    assert re.match(r"^[a-z0-9-]{1,64}$", meta["name"]) and meta["name"] == SKILL.parent.name
    assert 0 < len(meta["description"]) <= 1024 and not re.search("[<>]", meta["description"])
    assert len(body.splitlines()) < 500
    evals = json.loads((SKILL.parent / "evals" / "evals.json").read_text())
    assert len(evals) >= 3 and all(e["query"] and e["expected_behavior"] for e in evals)


def test_every_command_exists():
    text = SKILL.read_text()
    swarm_help = _help("swarm")
    ledger_help = _help("ledger")
    swarm = set(re.findall(r"agentihooks swarm <slug> (\w[\w-]*)", text))
    ledger = set(re.findall(r"agentihooks ledger(?: --slug <slug>)? (\w[\w-]*)", text))
    assert {"create", "start"} <= swarm
    assert {"new", "task"} <= ledger
    assert all(re.search(rf"\b{c}\b", swarm_help) for c in swarm)
    assert all(re.search(rf"\b{c}\b", ledger_help) for c in ledger - {"new"})


def test_the_skill_describes_stacked_claims_and_territory_as_order_only():
    text = " ".join(SKILL.read_text().split())
    assert "claimed or in review with a recorded branch" in text
    assert "`swarm park`" in text and "`swarm restack`" in text
    assert "Territory only orders claims" in text
    assert "never while its territory overlaps" not in text


def test_a_plan_continuing_a_ledger_appends_its_phases_with_a_parsed_command():
    from scripts.swarm_ledger import ledger

    [line] = re.findall(r"^agentihooks ledger (--slug <slug> --as <name> plan phases \S+)$", SKILL.read_text(), re.M)
    args = ledger.build_parser().parse_args(line.split())
    assert (args.command, args.action, args.path) == ("plan", "phases", "<phases.json>")


def test_manual_phase_tasks_come_from_a_published_plan_with_slice_anchors():
    from scripts.swarm_ledger import ledger

    text = SKILL.read_text()
    [publish] = re.findall(r"^agentihooks ledger (--slug <slug> --as <name> publish-plan \S+ --phase \S+)$", text, re.M)
    args = ledger.build_parser().parse_args(publish.split())
    assert (args.command, args.path, args.phase) == ("publish-plan", "<plan-file>", "<phase-ids>")
    assert "<!-- slice: <id> -->" in text
    assert re.search(r"^agentihooks ledger --slug <slug> task add .*--plan-slice <id>", text, re.M)


def test_the_sweep_template_uses_flags_the_sweep_parser_accepts():
    from scripts.trace_cli import build_sweep_parser

    text = (SKILL.parent / "work-beyond-code.md").read_text()
    template = text.split("## Template: sweep a corrected directive", 1)[1]
    assert "--kind troubleshoot" in template
    runs = re.findall(r"agentihooks trace sweep((?: --[a-z]+(?: <slug>)?)*)", template)
    assert {"", " --apply --ledger <slug>"} <= set(runs)
    for run in runs:
        build_sweep_parser().parse_args(run.replace("<slug>", "s").split())


def test_create_names_the_template_flag_the_parser_accepts():
    from scripts.swarm.cli import build_parser

    text = SKILL.read_text()
    assert re.search(r"agentihooks swarm <slug> create .*--template <name>", text)
    assert "agentihooks swarm templates" in text
    assert build_parser().parse_args(["sw", "create", "--repo", "/r", "--template", "codex-ci"]).template == "codex-ci"


def test_the_final_message_hands_the_operator_the_ledger_link():
    final = SKILL.read_text().split("## Hand over to the master", 1)[1]
    assert "agentihooks swarm <slug> url" in final
    assert "Ledger page:" in final and "final message" in final
