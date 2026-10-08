import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_package_internal_names import LEAKS
from tests.test_package_role_skills import PARSERS, _argv, _commands
from tests.test_profile_render import world as render_world

world = render_world
ROLES = Path(__file__).resolve().parents[1] / "profiles" / "package" / "roles"
SKILL = ROLES / "master" / ".claude" / "skills" / "prompt-user-parameter"
PLANTED = "planted-" + "operator-value"
PANE = "w1:p9"
FAKE_HERDR = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$HERDR_LOG"
if [[ "$1 $2" == "pane split" ]]; then
  printf '{"result":{"pane":{"pane_id":"w1:p9"}}}\\n'
fi
"""


@pytest.fixture
def box(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    herdr = bin_dir / "herdr"
    herdr.write_text(FAKE_HERDR)
    herdr.chmod(0o755)
    inputs = tmp_path / "scratchpad" / "agentihooks" / "task" / "inputs"
    inputs.mkdir(parents=True)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TERM": "dumb",
        "HERDR_LOG": str(tmp_path / "herdr.log"),
    }
    return {"root": tmp_path, "inputs": inputs, "env": env, "log": tmp_path / "herdr.log"}


def _herdr_calls(box) -> list[str]:
    return box["log"].read_text().splitlines() if box["log"].exists() else []


def _run(box, body: str, stdin: str = ""):
    script = box["inputs"] / "key.sh"
    script.write_text(body)
    status = box["inputs"] / "key.status"
    done = subprocess.run(
        ["bash", str(SKILL / "scripts" / "run.sh"), str(script), str(status), PANE],
        input=stdin,
        capture_output=True,
        text=True,
        env=box["env"],
        timeout=30,
    )
    return script, status, done


def test_success_writes_done_deletes_the_script_and_closes_the_pane(box):
    seen = box["inputs"] / "length"
    body = f'read_secret TOKEN "Tailnet key"\nprintf "%s" "${{#TOKEN}}" > {seen}\n'

    script, status, done = _run(box, body, f"{PLANTED}\n")

    assert status.read_text() == "DONE\n"
    assert not script.exists()
    assert seen.read_text() == str(len(PLANTED))
    assert _herdr_calls(box) == [f"pane close {PANE}"]
    assert PLANTED not in done.stdout + done.stderr


def test_failure_writes_error_with_its_exit_code(box):
    body = 'read_secret TOKEN "Tailnet key"\nexit 7\n'

    script, status, done = _run(box, body, f"{PLANTED}\n")

    assert status.read_text() == "ERROR 7\n"
    assert not script.exists()
    assert _herdr_calls(box) == [f"pane close {PANE}"]
    assert PLANTED not in status.read_text() + done.stdout + done.stderr


def test_an_empty_value_stops_the_script_with_an_error(box):
    after = box["inputs"] / "after"
    body = f'read_secret TOKEN "Tailnet key"\ntouch {after}\n'

    script, status, _ = _run(box, body, "\n")

    assert status.read_text() == "ERROR 1\n"
    assert not after.exists()
    assert not script.exists()


@pytest.mark.parametrize("name", ["value", "name", "prompt"])
def test_a_variable_named_like_the_helper_locals_still_reaches_the_script(box, name):
    seen = box["inputs"] / "length"
    body = f'read_secret {name} "Tailnet key"\nprintf "%s" "${{#{name}}}" > {seen}\n'

    _, status, done = _run(box, body, f"{PLANTED}\n")

    assert status.read_text() == "DONE\n"
    assert seen.read_text() == str(len(PLANTED))
    assert PLANTED not in done.stdout + done.stderr


@pytest.mark.parametrize("call", ['read_secret "Tailnet key"', 'read_secret __rs_name "Tailnet key"'])
def test_a_missing_or_reserved_variable_name_fails_without_reading_a_value(box, call):
    body = f"{call}\n"

    _, status, done = _run(box, body, f"{PLANTED}\n")

    assert status.read_text() == "ERROR 2\n"
    assert "read_secret needs a variable name first" in done.stderr
    assert PLANTED not in done.stdout + done.stderr


def test_the_value_never_reaches_the_status_file_or_any_file_beside_it(box):
    body = 'read_secret TOKEN "Tailnet key"\ntrue\n'

    _, status, _ = _run(box, body, f"{PLANTED}\n")

    assert status.read_text() == "DONE\n"
    leaked = [p.name for p in box["root"].rglob("*") if p.is_file() and PLANTED in p.read_text(errors="ignore")]
    assert leaked == []


def _open(box, script: Path, extra: dict | None = None):
    return subprocess.run(
        ["bash", str(SKILL / "scripts" / "open.sh"), str(script), "Tailnet key"],
        capture_output=True,
        text=True,
        env={**box["env"], **(extra or {})},
        timeout=30,
    )


def test_open_splits_a_pane_beside_the_caller_and_runs_the_runner_there(box):
    script = box["inputs"] / "key.sh"
    script.write_text("true\n")

    done = _open(box, script, {"HERDR_PANE_ID": "w1:p1"})

    status = box["inputs"] / "key.status"
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f"pane={PANE}", f"status={status}"]
    calls = _herdr_calls(box)
    assert calls[0] == f"pane split --pane w1:p1 --direction right --cwd {box['inputs']}"
    assert calls[1] == f"pane rename {PANE} Tailnet key"
    assert calls[2] == f"pane run {PANE} bash {SKILL / 'scripts' / 'run.sh'} {script} {status} {PANE}"
    assert len(calls) == 3


def test_open_refuses_a_script_outside_the_scratch_folder(box):
    outside = box["root"] / "tmp" / "key.sh"
    outside.parent.mkdir()
    outside.write_text("true\n")

    done = _open(box, outside, {"HERDR_PANE_ID": "w1:p1"})

    assert done.returncode == 2
    assert done.stderr == f"refused: {outside} is outside {box['root'] / 'scratchpad'}\n"
    assert _herdr_calls(box) == []


def test_open_refuses_a_caller_outside_a_herdr_pane(box):
    script = box["inputs"] / "key.sh"
    script.write_text("true\n")

    done = _open(box, script)

    assert done.returncode == 2
    assert done.stderr == "refused: run from inside a herdr pane (HERDR_PANE_ID is unset)\n"
    assert _herdr_calls(box) == []


def test_the_skill_passes_the_skill_gate():
    _, front, body = (SKILL / "SKILL.md").read_text().split("---", 2)
    meta = yaml.safe_load(front)

    assert meta["name"] == SKILL.name
    assert 0 < len(meta["description"]) <= 1024 and not re.search("[<>]", meta["description"])
    assert len(body.splitlines()) < 500
    evals = json.loads((SKILL / "evals" / "evals.json").read_text())
    assert len(evals) >= 3 and all(e["query"] and e["expected_behavior"] for e in evals)
    leaks = {p.name: LEAKS.findall(p.read_text()) for p in SKILL.rglob("*") if p.is_file()}
    assert {name: found for name, found in leaks.items() if found} == {}


def test_the_master_rule_says_when_to_use_the_skill():
    rule = (ROLES / "master" / ".claude" / "rules" / "master-role.md").read_text()
    line = next(line for line in rule.splitlines() if "prompt-user-parameter" in line)

    assert "operator on" in line and "Priorities" in line and "Monitor" in line
    for command in _commands(line):
        tool, argv = _argv(command)
        assert PARSERS[tool](argv).values == ["x", "x"]


@pytest.mark.parametrize("role", sorted(p.name for p in ROLES.iterdir() if not p.name.startswith("_")))
def test_only_the_master_home_lists_the_skill(world, role):
    from scripts.profiles import render

    world["install"]._save_state({})
    out = render.render_claude(role)

    listed = {p.name for p in (out / "skills").iterdir()}
    assert ("prompt-user-parameter" in listed) == (role == "master")
