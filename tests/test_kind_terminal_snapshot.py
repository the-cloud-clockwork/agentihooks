import json
import re
import subprocess
from pathlib import Path
from string import Template

import pytest


def _snapshot_bases(source: str) -> list[str]:
    arguments: dict[str, str] = {}
    stages: dict[str, str] = {}
    failures: list[str] = []
    heredocs: list[str] = []
    base = ""
    stage = ""
    count = 0
    for line in re.sub(r"\\\r?\n", " ", source).splitlines():
        if heredocs:
            if line.strip() == heredocs[0]:
                heredocs.pop(0)
        else:
            words = line.strip().split(maxsplit=1)
            if len(words) != 2 or words[0].startswith("#"):
                continue
            instruction, value = words[0].upper(), words[1]
            if instruction == "ARG" and not count:
                name, _, default = value.partition("=")
                arguments[name] = Template(default.strip("\"'")).safe_substitute(arguments)
            if instruction == "FROM":
                fields = value.split()
                reference = next(field for field in fields if not field.startswith("--"))
                reference = Template(reference).safe_substitute(arguments)
                base = stages.get(reference.lower(), reference)
                stage = fields[-1] if len(fields) >= 3 and fields[-2].upper() == "AS" else str(count)
                stages[str(count)] = base
                stages[stage.lower()] = base
                count += 1
            if instruction in {"RUN", "COPY", "ADD"}:
                heredocs = [
                    match[2]
                    for match in re.finditer(r"""'[^']*'|"(?:\\.|[^"\\])*"|\\.|<<-?\s*(['"]?)(\w+)\1""", value)
                    if match[2]
                ]
        if count and re.search(r"snapshot\.debian\.org|debian_snapshot", line, re.I):
            pinned = re.search(r"@sha256:[a-f0-9]{64}$|:\d+\.\d+\.\d+[-+._a-z0-9]*$", base, re.I)
            if not pinned and stage not in failures:
                failures.append(stage)
    return failures


@pytest.mark.feature("kind-terminal-snapshot-base")
@pytest.mark.parametrize(
    "case",
    json.loads((Path(__file__).parent / "fixtures/snapshot_bases.json").read_text()),
    ids=lambda case: case["name"],
)
def test_snapshot_stage_guard_fixtures(case: dict[str, str | list[str]]) -> None:
    assert _snapshot_bases(str(case["source"])) == case["failures"]


@pytest.mark.feature("kind-terminal-snapshot-base")
def test_every_dockerfile_snapshot_stage_has_a_pinned_base() -> None:
    root = Path(__file__).parents[1]
    paths = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True).stdout
    failures = {}
    for name in paths.split("\0"):
        path = Path(name)
        if not (path.name.startswith("Dockerfile") or path.name.endswith(".Dockerfile")):
            continue
        if path.name.endswith(".dockerignore") or any(part.startswith(".semgrep") for part in path.parts):
            continue
        stages = _snapshot_bases((root / path).read_text())
        if stages:
            failures[name] = stages
    assert not failures, f"Debian snapshot stages have floating bases: {failures}"


@pytest.mark.feature("kind-terminal-snapshot-base")
@pytest.mark.parametrize("source", ["terminal-worker.Dockerfile", "kind-smoke.sh"])
def test_terminal_packages_and_prefetch_use_the_locked_python(source: str) -> None:
    root = Path(__file__).parents[1]
    lock = json.loads((root / "docker/swarm-node/versions.lock").read_text())
    image = f"mirror.gcr.io/library/python:{lock['python']}-slim-bookworm"
    text = (root / "deploy/helm/agentihooks-swarm/ci" / source).read_text()
    assert image in text
    assert text.count(image) == 1
    assert "python:3.12-slim-bookworm" not in text
