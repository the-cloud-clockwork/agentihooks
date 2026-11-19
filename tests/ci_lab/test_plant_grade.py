import hashlib
import hmac
import io
import json
import subprocess
import sys
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest

from scripts.ci_lab import head_notice, plant_grade
from scripts.ci_lab.digests import GradeInputs, grade_key
from scripts.ci_lab.webhooks import NOTICE_SECRET_ENV

pytestmark = [pytest.mark.unit, pytest.mark.feature("lab-static-plant-proofs")]

REPOSITORY = "https://github.com/acme/widgets"
ENVIRON = {
    head_notice.URL_ENV: "https://receiver.invalid/lab/head",
    head_notice.ACTOR_ENV: "plant-proof",
    NOTICE_SECRET_ENV: "unit-proof-only",
}


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Proof")
    git(root, "config", "user.email", "proof@example.invalid")
    target = root / "sample.py"
    target.write_text("before\n")
    catalog = root / "tests/ci_lab/plants"
    catalog.mkdir(parents=True)
    (catalog / "lint.json").write_text(
        json.dumps(
            {
                "gate": "lint",
                "reason": "I001",
                "edits": [
                    {"path": "sample.py", "before": "before", "after": "after"},
                ],
            }
        )
    )
    git(root, "add", "sample.py", "tests/ci_lab/plants/lint.json")
    git(root, "commit", "-qm", "Seed")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    git(root, "remote", "add", "origin", str(remote))
    return root


class Answer(io.BytesIO):
    status = 202


class Lab:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.notices: list[Mapping[str, str]] = []
        self.now = 0.0
        self.answer = 202
        self.failure = "lint"
        self.required = "failure"
        self.incomplete = False
        self.pages: list[str] = []

    def open(self, request: Request, timeout: float) -> Answer:
        assert timeout > 0
        body = request.data
        assert isinstance(body, bytes)
        assert request.full_url == ENVIRON[head_notice.URL_ENV]
        assert request.get_header("X-hub-signature-256") == (
            "sha256=" + hmac.new(ENVIRON[NOTICE_SECRET_ENV].encode(), body, hashlib.sha256).hexdigest()
        )
        fields = json.loads(body)
        assert git(self.root, "ls-remote", "origin", "refs/heads/diffcheck/ci-lab-lint").split()[0] == fields["head"]
        self.notices.append(fields)
        answer = Answer()
        answer.status = self.answer
        return answer

    def fetch(self, path: str) -> Mapping[str, Any]:
        self.pages.append(path)
        assert path.startswith("repos/acme/widgets/commits/")
        assert "filter=all" in path
        fields = self.notices[-1]
        head, base = fields["head"], fields["base"]
        run = grade_key(GradeInputs(REPOSITORY, head, base, "pull_request", base))
        red = len(self.notices) == 2
        rows = [
            self.check("lab-shadow/required", "success", "other-grade"),
            self.check("lab-shadow/lint", "failure", "other-grade"),
            self.check("Gate — Required", "success", run),
            self.check("lab-shadow/required", self.required if red else "success", run),
            self.check("lab-shadow/" + (self.failure if red else "lint"), "failure" if red else "success", run),
        ]
        if self.incomplete:
            rows[-2]["status"] = "in_progress"
        return {"check_runs": rows}

    def check(self, name: str, conclusion: str, run: str) -> dict[str, Any]:
        return {
            "name": name,
            "conclusion": conclusion,
            "external_id": run,
            "status": "completed",
            "html_url": "https://github.com/acme/widgets/runs/" + name.rsplit("/", 1)[-1],
        }

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def ports(self) -> plant_grade.Ports:
        return plant_grade.Ports(self.fetch, self.open, self.sleep, self.clock)


@pytest.mark.feature("lab-static-plant-proofs")
def test_two_signed_heads_grade_against_one_base_and_ignore_other_grades(repository: Path) -> None:
    lab = Lab(repository)
    base = git(repository, "rev-parse", "HEAD")
    result = plant_grade.prove(repository, "lint", base, REPOSITORY, ENVIRON, lab.ports())
    assert result == {
        "plant": "lint",
        "gate": "lint",
        "reason": "I001",
        "base": base,
        "green": "https://github.com/acme/widgets/runs/required",
        "red": "https://github.com/acme/widgets/runs/required",
    }
    assert len(lab.notices) == 2
    assert all(
        notice["base"] == base and notice["repo"] == REPOSITORY and notice["actor"] == "plant-proof"
        for notice in lab.notices
    )
    assert git(repository, "show", f"{lab.notices[0]['head']}:sample.py") == "before"
    assert git(repository, "show", f"{lab.notices[1]['head']}:sample.py") == "after"
    assert git(repository, "rev-parse", "HEAD") == base


@pytest.mark.parametrize("answer", [0, 200, 403, 500])
@pytest.mark.feature("lab-static-plant-proofs")
def test_a_refused_notice_cannot_be_used_as_a_proof(repository: Path, answer: int) -> None:
    lab = Lab(repository)
    lab.answer = answer
    with pytest.raises(ValueError, match="notice"):
        plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, ENVIRON, lab.ports())
    assert len(lab.notices) == 1
    assert lab.pages == []


@pytest.mark.parametrize("failure,required", [("type-gate", "failure"), ("lint", "success")])
@pytest.mark.feature("lab-static-plant-proofs")
def test_a_wrong_red_gate_or_a_green_plant_is_refused(repository: Path, failure: str, required: str) -> None:
    lab = Lab(repository)
    lab.failure, lab.required = failure, required
    with pytest.raises(ValueError, match="plant"):
        plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, ENVIRON, lab.ports())


@pytest.mark.feature("lab-static-plant-proofs")
def test_an_incomplete_grade_expires_without_pushing_the_plant(repository: Path) -> None:
    lab = Lab(repository)
    lab.incomplete = True
    with pytest.raises(TimeoutError, match="grade"):
        plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, ENVIRON, lab.ports(), timeout_s=2)
    assert len(lab.notices) == 1
    assert lab.now == 2


@pytest.mark.feature("lab-static-plant-proofs")
def test_a_completed_pair_can_be_proven_again_without_rewinding_its_remote(repository: Path) -> None:
    first, second = Lab(repository), Lab(repository)
    a = plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, ENVIRON, first.ports())
    b = plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, ENVIRON, second.ports())
    assert a == b
    assert first.notices == second.notices


@pytest.mark.feature("lab-static-plant-proofs")
def test_the_module_dispatch_exposes_the_plant_command() -> None:
    answer = subprocess.run(
        [sys.executable, "-m", "scripts.ci_lab", "grade", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert answer.returncode == 0, answer.stdout + answer.stderr
    assert "--plant" in answer.stdout


@pytest.mark.feature("lab-static-plant-proofs")
def test_the_command_prints_links_and_appends_the_proof_summary(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repository)
    lab = Lab(repository)
    summary = tmp_path / "summary.md"
    summary.write_text("Earlier proof\n")
    environ = {**ENVIRON, "GITHUB_STEP_SUMMARY": str(summary)}
    assert (
        plant_grade.main(
            ["--plant", "lint", "--base", "HEAD", "--repository", REPOSITORY],
            environ,
            lab.ports(),
        )
        == 0
    )
    row = json.loads(capsys.readouterr().out)
    assert row["plant"] == "lint"
    assert row["green"] == row["red"] == "https://github.com/acme/widgets/runs/required"
    text = summary.read_text()
    assert text.startswith("Earlier proof\n")
    assert "lint" in text and "I001" in text
    assert "[green](https://github.com/acme/widgets/runs/required)" in text
    assert "[red](https://github.com/acme/widgets/runs/required)" in text


@pytest.mark.feature("lab-static-plant-proofs")
def test_the_command_uses_real_transport_adapters_and_normalizes_the_remote(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repository)
    lab = Lab(repository)
    real_run = subprocess.run

    def transport(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if arguments[3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(arguments, 0, "git@github.com:Acme/widgets.git\n", "")
        if Path(arguments[0]).name == "git":
            return real_run(arguments, **kwargs)
        assert "api" in arguments
        path = arguments[arguments.index("api") + 1]
        return subprocess.CompletedProcess(arguments, 0, json.dumps(lab.fetch(path)), "")

    monkeypatch.setattr(subprocess, "run", transport)
    monkeypatch.setattr(urllib.request, "urlopen", lab.open)
    assert plant_grade.main(["--plant", "lint", "--base", "HEAD"], ENVIRON) == 0
    assert json.loads(capsys.readouterr().out)["plant"] == "lint"
    assert len(lab.notices) == 2
    assert all(notice["repo"] == REPOSITORY for notice in lab.notices)


@pytest.mark.feature("lab-static-plant-proofs")
def test_a_refused_command_prints_no_success_row_or_summary(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repository)
    lab = Lab(repository)
    lab.answer = 403
    summary = tmp_path / "summary.md"
    assert (
        plant_grade.main(
            ["--plant", "lint", "--base", "HEAD", "--repository", REPOSITORY],
            {**ENVIRON, "GITHUB_STEP_SUMMARY": str(summary)},
            lab.ports(),
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "notice" in output.err
    assert not summary.exists()
