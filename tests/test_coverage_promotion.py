import json
import subprocess
from pathlib import Path

import pytest
import yaml
from coverage import CoverageData

from tests import coverage_baseline as baseline
from tests import coverage_ratchet as ratchet
from tests.coverage_grade import measured_lines

pytestmark = pytest.mark.timeout(30, method="signal")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, name):
    (repo / "head.txt").write_text(name)
    _git(repo, "add", "hooks/example.py", "head.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


def _publish(job, result):
    condition = job["if"].removeprefix("${{").removesuffix("}}")
    for expression, value in {
        "!cancelled()": "1 == 1",
        "github.event_name": "'push'",
        "needs.unit.result": "'success'",
        "needs.coverage-ratchet.result": repr(result),
    }.items():
        condition = condition.replace(expression, value)
    process = subprocess.run(["bash", "-c", f"[[ {condition} ]]"], capture_output=True, text=True)
    assert process.returncode in {0, 1}, process.stderr
    return process.returncode == 0


def _summarize_database(shard: Path, summary: Path) -> None:
    """Write the executed lines summary a unit shard uploads for its own database."""
    summary.parent.mkdir(parents=True, exist_ok=True)
    lines = {path: sorted(ran) for path, ran in measured_lines(shard).items()}
    summary.write_text(json.dumps({baseline.OWN_DATABASE: lines}))


def test_a_coverage_loss_stays_red_on_the_next_promotion_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setattr(ratchet, "_cpus", lambda: 1)
    workflow = Path(__file__).parents[1] / ".github/workflows/test.yml"
    publisher = yaml.safe_load(workflow.read_text())["jobs"]["coverage-baseline"]
    repo = tmp_path / "repo"
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks/example.py").write_text("value = 1\nvalue += 1\n")
    _git(repo, "init", "-q")
    _commit(repo, "initial")
    shards = tmp_path / "shards"
    shard = shards / "coverage-3.12-1/.coverage"
    shard.parent.mkdir(parents=True)
    data = CoverageData(basename=str(shard))
    data.add_lines({"hooks/example.py": {1, 2}})
    data.write()
    summary = tmp_path / "summaries/executed-lines-3.12-1/executed-lines.json"
    _summarize_database(shard, summary)
    stored = tmp_path / "baseline.json"
    baseline.record(repo, [summary], stored)
    statuses = []
    publications = []
    measured_bases = []
    heads = []
    for name, lines in [("control", {1, 2}), ("loss", {1}), ("next", {1}), ("repair", {1, 2})]:
        heads.append(_commit(repo, name))
        measured_base = baseline.read(stored)["commit"]
        measured_bases.append(measured_base)
        data.erase()
        data.add_lines({"hooks/example.py": lines})
        data.write()
        _summarize_database(shard, summary)
        out = tmp_path / name
        status = ratchet.main(
            [
                "--base",
                measured_base,
                "--head",
                str(repo),
                "--shards",
                "1",
                "--head-shards",
                str(shards),
                "--out",
                str(out),
                "--base-baseline",
                str(stored),
            ]
        )
        statuses.append(status)
        published = _publish(publisher, "success" if status == 0 else "failure")
        publications.append(published)
        if published:
            baseline.record(repo, [summary], stored, stored)
        print(f"{name}: ratchet={status}, published={published}, measured_base={measured_base}")
    assert statuses == [0, 1, 1, 0]
    assert publications == [True, False, False, True]
    assert measured_bases[1:] == [heads[0]] * 3
    assert "hooks/example.py:2 ran on base" in (tmp_path / "loss/report.txt").read_text()
    assert "hooks/example.py:2 ran on base" in (tmp_path / "next/report.txt").read_text()
    assert baseline.read(stored)["commit"] == heads[-1]
    assert [entry["commit"] for entry in baseline.read(stored)["history"]] == [heads[0], measured_bases[0]]
