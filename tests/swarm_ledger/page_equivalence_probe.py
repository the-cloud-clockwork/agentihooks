import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


@pytest.mark.timeout(300)
def test_candidate_page_replay_rejects_a_dropped_chat_recipient(tmp_path):
    root = Path(__file__).resolve().parents[2]
    archive = tmp_path / "source.tar"
    with archive.open("wb") as stream:
        subprocess.run(["git", "archive", "HEAD"], cwd=root, stdout=stream, check=True)
    with tarfile.open(archive) as source:
        source.extractall(tmp_path / "base", filter="data")
    shutil.copytree(tmp_path / "base", tmp_path / "head")
    replay = tmp_path / "head" / "tests" / "swarm_ledger" / "equivalence.sh"
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "browsers")}
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], env=env, check=True)
    subprocess.run(["bash", str(replay), "plant"], cwd=tmp_path, env=env, check=True)
    subprocess.run(["bash", str(replay), "page"], cwd=tmp_path, env=env, check=True)
    fault = subprocess.run(
        ["cmp", "base-page.json", "fault-page.json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    print(f"Planted real page change: exit {fault.returncode}; {fault.stdout.strip()}")
    assert fault.returncode == 1
