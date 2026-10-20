import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]

_DECLARED = [
    *sorted(path.relative_to(_ROOT).as_posix() for path in (_ROOT / ".github/workflows").glob("*.yml")),
    ".github/input-keys.json",
    "README.md",
    "docs/getting-started/index.md",
    "docs/getting-started/compose-quick-start.md",
    "profiles/package/skills/swarm-ledger/SKILL.md",
    "profiles/package/skills/read-docs/SKILL.md",
]


@pytest.mark.parametrize("path", _DECLARED)
def test_no_ci_job_or_doc_names_python_311(path):
    text = (_ROOT / path).read_text()
    assert not re.search(r"(?<![\d.])3\.11(?!\d)|py311", text)


def test_the_package_requires_python_312():
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert project["project"]["requires-python"] == ">=3.12"
    assert "Programming Language :: Python :: 3.11" not in project["project"]["classifiers"]
    assert project["tool"]["ruff"]["target-version"] == "py312"


def test_no_python_311_duration_file_remains():
    assert not (_ROOT / ".test_durations-3.11").exists()
