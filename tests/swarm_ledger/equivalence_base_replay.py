import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("page_difference", [False, True], ids=["stale-base", "page-fault"])
@pytest.mark.timeout(300)
def test_stale_pull_request_page_comparison(tmp_path, page_difference):
    root = Path(__file__).resolve().parents[2]
    head = tmp_path / "head"
    archive = tmp_path / "source.tar"
    with archive.open("wb") as stream:
        subprocess.run(["git", "archive", "HEAD"], cwd=root, stdout=stream, check=True)
    with tarfile.open(archive) as source:
        source.extractall(head, filter="data")

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=CI", "-c", "user.email=ci@example.invalid", *args],
            cwd=head,
            text=True,
        ).strip()

    git("init", "-b", "dev")
    git("add", ".")
    git("commit", "-qm", "Initial page")
    event_base = git("rev-parse", "HEAD")
    git("switch", "-c", "pr")
    needle = 'return to && !text.startsWith("@") ? `@${to} ${text}` : text;'
    if page_difference:
        chat = head / "scripts/swarm_ledger/static/js/chat.js"
        source = chat.read_text()
        assert source.count(needle) == 1
        chat.write_text(source.replace(needle, "return text;"))
    git("add", ".")
    git("commit", "--allow-empty", "-qm", "Open pull request before dev panel")
    git("switch", "dev")
    shell = head / "scripts/swarm_ledger/shell.html"
    source = shell.read_text()
    assert source.count("<body>") == 1
    shell.write_text(source.replace("<body>", '<body><aside id="new-dev-panel">New dev panel</aside>'))
    git("add", ".")
    git("commit", "-qm", "Add unrelated dev panel")
    dev = git("rev-parse", "HEAD")
    git("merge", "--no-ff", "pr", "-m", "Test pull request merge")
    workflow = yaml.safe_load((head / ".github/workflows/equivalence.yml").read_text())
    selector = next(
        step for step in workflow["jobs"]["ledger-equivalence"]["steps"] if step.get("id") == "comparison-base"
    )
    output = tmp_path / "output"
    env = {**os.environ, "GITHUB_OUTPUT": str(output), "RUNNER_TEMP": str(tmp_path / "logs")}
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", selector["run"]], cwd=tmp_path, env=env, check=True)
    selected = dict(line.split("=", 1) for line in output.read_text().splitlines())["sha"]
    assert selected == dev and selected != event_base
    with archive.open("wb") as stream:
        subprocess.run(["git", "archive", selected], cwd=head, stdout=stream, check=True)
    with tarfile.open(archive) as source:
        source.extractall(tmp_path / "base", filter="data")
    fault = tmp_path / "fault-chat"
    shutil.copytree(tmp_path / "base", fault)
    chat = fault / "scripts/swarm_ledger/static/js/chat.js"
    source = chat.read_text()
    assert source.count(needle) == 1
    chat.write_text(source.replace(needle, "return text;"))
    (tmp_path / "logs").mkdir()
    result = subprocess.run(
        ["bash", str(tmp_path / "base/tests/swarm_ledger/equivalence.sh"), "page"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    print(f"Event base {event_base}; tested dev {dev}; selected {selected}; page difference {page_difference}")
    print(result.stdout + result.stderr)
    for log in sorted((tmp_path / "logs").glob("*.log")):
        print(log.read_text())
    if page_difference:
        assert result.returncode == 1
        assert "base-page.json head-page.json differ" in result.stdout
    else:
        assert result.returncode == 0
        assert "identical base and head pages; planted chat fault rejected" in result.stdout
