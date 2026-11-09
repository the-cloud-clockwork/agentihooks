import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("page_difference", [False, True])
def test_equivalence_uses_dev_parent_of_tested_merge(tmp_path, page_difference):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/equivalence.yml").read_text())
    steps = workflow["jobs"]["ledger-equivalence"]["steps"]
    head = tmp_path / "head"
    head.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=CI", "-c", "user.email=ci@example.invalid", *args],
            cwd=head,
            text=True,
        ).strip()

    git("init", "-b", "dev")
    page = head / "page.html"
    page.write_text("<main>Ledger</main>\n")
    git("add", ".")
    git("commit", "-m", "Initial page")
    event_base = git("rev-parse", "HEAD")
    git("switch", "-c", "pr")
    if page_difference:
        page.write_text("<main>Changed ledger</main>\n")
    (head / "unrelated.txt").write_text("Pull request opened before the panel\n")
    git("add", ".")
    git("commit", "-m", "Open pull request")
    git("switch", "dev")
    (head / "brain.html").write_text("<aside>Brain context</aside>\n")
    git("add", ".")
    git("commit", "-m", "Add dev panel")
    current_dev = git("rev-parse", "HEAD")
    git("switch", "-c", "tested-merge")
    git("merge", "--no-ff", "pr", "-m", "Test pull request merge")
    git("switch", "dev")
    (head / "later.html").write_text("Later dev change\n")
    git("add", ".")
    git("commit", "-m", "Advance dev after checkout")
    git("switch", "tested-merge")

    selector = next(step for step in steps if step.get("id") == "comparison-base")
    output = tmp_path / "output"
    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", selector["run"]],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        check=True,
    )
    selected = dict(line.split("=", 1) for line in output.read_text().splitlines())["sha"]
    assert selected == current_dev
    assert selected != event_base
    assert selected != git("rev-parse", "dev")
    checkout = next(step for step in steps if step.get("with", {}).get("path") == "base")
    assert checkout["with"]["ref"] == "${{ steps.comparison-base.outputs.sha }}"
    assert steps[0]["with"]["fetch-depth"] == 2
    assert git("show", f"{selected}:brain.html") == git("show", "HEAD:brain.html")
    assert (git("show", f"{selected}:page.html") != git("show", "HEAD:page.html")) == page_difference
