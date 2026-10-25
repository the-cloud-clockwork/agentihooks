import re
import shutil
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "profiles" / "package"
LEAKS = re.compile(
    r"\b(anton|smith|tcc|homeofanton|litellm|openbao|plane|manifesto|nestor|colt|nestorcolt|agenticore-app)\b"
    r"|gateway[ _-]tools|§|cloudclockwork"
    r"|\bclockwork\b(?!(?<=github\.com/the-cloud-clockwork)/agentihooks\b(?!-)"
    r"|(?<=//the-cloud-clockwork)\.github\.io/agentihooks\b(?!-))"
    r"|\b10\.\d+\.\d+\.\d+|\b192\.168\.\d+\.\d+|\b172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|[\w.+-]+@gmail\.com",
    re.IGNORECASE,
)


def internal_names(root: Path) -> dict[str, list[str]]:
    found = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if LEAKS.search(line):
                found.setdefault(str(path.relative_to(root)), []).append(f"{number}: {line.strip()}")
    return found


def test_package_names_nothing_internal():
    assert internal_names(PACKAGE) == {}


@pytest.mark.parametrize(
    "planted",
    [
        "the anton cluster",
        "Smith",
        "the tcc servers",
        "homeofanton",
        "LiteLLM",
        "openbao",
        "plane",
        "gateway tools",
        "gateway_tools",
        "host 10.0.0.12",
        "host 192.168.1.4",
        "host 172.20.3.9",
        "see the manifesto",
        "§4",
        "Nestor",
        "colt",
        "someone@gmail.com",
        "the-cloud-clockwork",
        "The-Cloud-Clockwork/antoncore",
        "https://github.com/the-cloud-clockwork/agentihooks-bundle/pull/1",
        "the actor nestorcolt",
        "agenticore-app[bot]",
        "tcc-main-ci[bot]",
        "runs-on: anton-oss-light",
        "the anton-oss-queue pool",
        "langfuse.homeofanton.com",
        "TheCloudClockwork",
        "repos/the-cloud-clockwork/agentihooks",
        "the-cloud-clockwork.github.io agentihooks",
        "clockwork/agentihooks",
    ],
)
def test_package_scan_turns_red_on_a_planted_name(tmp_path, planted):
    tree = tmp_path / "package"
    shutil.copytree(PACKAGE, tree, ignore=shutil.ignore_patterns("__pycache__"))
    (tree / "skills" / "planted.md").write_text(f"# Notes\n\nWritten for {planted}.\n")

    assert internal_names(tree)["skills/planted.md"] == [f"3: Written for {planted}."]


@pytest.mark.parametrize(
    "clean",
    [
        "planet",
        "airplane",
        "host 172.15.0.1",
        "10.5 seconds",
        "colts",
        "clockworks",
        "https://github.com/the-cloud-clockwork/agentihooks/pull/913",
        "https://the-cloud-clockwork.github.io/agentihooks/docs/",
    ],
)
def test_package_scan_passes_words_that_only_contain_a_name(clean):
    assert not LEAKS.search(clean)
