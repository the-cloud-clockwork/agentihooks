import json
import subprocess
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.ci_budget import rate_limit
from scripts.ci_lab import __main__ as entrypoint
from scripts.ci_lab import plant_grade, plant_proofs, plant_publish
from scripts.ci_lab.digests import GradeInputs, grade_key
from scripts.ci_lab.webhooks import PLANT_PREFIX
from tests.ci_lab.test_plant_publish import GitHub

pytestmark = pytest.mark.unit

REPOSITORY = "https://github.com/acme/widgets"


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
    (root / "sample.py").write_text("before\n")
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
    (catalog / "gate-script.json").write_text(
        (catalog / "lint.json").read_text().replace('"I001"', '"forbidden comment"')
    )
    git(root, "add", "sample.py", "tests/ci_lab/plants/lint.json", "tests/ci_lab/plants/gate-script.json")
    git(root, "commit", "-qm", "Seed")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    git(root, "remote", "add", "origin", str(remote))
    return root


class Lab:
    def __init__(self, root: Path, name: str = "lint") -> None:
        self.root = root
        self.name = name
        self.base = git(root, "rev-parse", "HEAD")
        self.heads: list[str] = []
        self.now = 0.0
        self.failure = "lint"
        self.required = "failure"
        self.incomplete = False
        self.pending_first = False
        self.twin_required = "success"
        self.seen: dict[str, int] = {}
        self.unrelated = False
        self.full_first_page = False
        self.pages: list[str] = []

    def published(self, head: str) -> bool:
        tips = git(self.root, "ls-remote", "--heads", "origin", f"refs/heads/diffcheck/lab-plant-{self.name}-*")
        return any(
            subprocess.run(
                ["git", "-C", str(self.root), "merge-base", "--is-ancestor", head, line.split()[0]],
                capture_output=True,
                check=False,
            ).returncode
            == 0
            for line in tips.splitlines()
        )

    def fetch(self, path: str, timeout_s: float = 900) -> Mapping[str, Any]:
        self.pages.append(path)
        assert len(self.pages) <= 8
        assert path.startswith("repos/acme/widgets/commits/")
        assert "filter=all" in path
        head = path.split("/")[4]
        assert self.published(head)
        if head not in self.heads:
            self.heads.append(head)
        red = git(self.root, "show", f"{head}:sample.py") == "after"
        run = grade_key(GradeInputs(REPOSITORY, head, self.base, "pull_request", self.base))
        if self.full_first_page and path.endswith("page=1"):
            return {"check_runs": [self.check("lab-shadow/lint", "failure", "other-grade")] * 100}
        rows = [
            self.check("lab-shadow/required", "success", "other-grade"),
            self.check("lab-shadow/lint", "failure", "other-grade"),
            self.check("Gate — Required", "success", run),
            self.check("lab-shadow/required", self.required if red else self.twin_required, run),
            self.check("lab-shadow/" + (self.failure if red else "lint"), "failure" if red else "success", run),
        ]
        self.seen[head] = self.seen.get(head, 0) + 1
        if self.incomplete or self.pending_first and self.seen[head] == 1:
            rows[-2]["status"] = "in_progress"
        rows.append(self.check("lab-shadow/required/extra", "success", run))
        rows.append(self.check("lab-shadow/reusable/required", "success", run))
        if self.unrelated:
            for row in rows:
                row["external_id"] = "other-grade"
        return {"check_runs": rows}

    def check(self, name: str, conclusion: str, run: str) -> dict[str, Any]:
        return {
            "name": name,
            "conclusion": conclusion,
            "external_id": run,
            "status": "completed",
            "output": {
                "text": (
                    "sample.py:1:1: I001 Import block is un-sorted or un-formatted"
                    if self.name == "lint"
                    else "::error::sample.py:1: forbidden comment"
                )
            },
            "html_url": "https://github.com/acme/widgets/runs/" + name.rsplit("/", 1)[-1],
        }

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def push(self, root: Path, pair: plant_proofs.Pair, head: str, repository: str) -> None:
        branch = f"{PLANT_PREFIX}{pair.name}-{pair.base}"
        remote = git(root, "ls-remote", "--heads", "origin", branch).split()
        if not remote or remote[0] not in {head, pair.red}:
            git(root, "push", "origin", f"{head}:{branch}")

    def ports(self) -> plant_grade.Ports:
        return plant_grade.Ports(self.fetch, self.sleep, self.clock, self.push)


@pytest.mark.parametrize("name", ["lint", "gate-script"])
@pytest.mark.feature("lab-static-plant-proofs")
def test_two_pushes_grade_against_one_protected_base_and_ignore_other_grades(repository: Path, name: str) -> None:
    lab = Lab(repository, name)
    base = git(repository, "rev-parse", "HEAD")
    result = plant_grade.prove(repository, name, base, REPOSITORY, lab.ports())
    assert result == {
        "plant": name,
        "gate": "lint",
        "reason": "I001" if name == "lint" else "forbidden comment",
        "base": base,
        "green": "https://github.com/acme/widgets/runs/required",
        "red": "https://github.com/acme/widgets/runs/required",
        "green_run": grade_key(GradeInputs(REPOSITORY, lab.heads[0], base, "pull_request", base)),
        "red_run": grade_key(GradeInputs(REPOSITORY, lab.heads[1], base, "pull_request", base)),
    }
    assert len(lab.heads) == 2
    assert git(repository, "show", f"{lab.heads[0]}:sample.py") == "before"
    assert git(repository, "show", f"{lab.heads[1]}:sample.py") == "after"
    assert git(repository, "rev-parse", "HEAD") == base


@pytest.mark.feature("lab-static-plant-proofs")
def test_a_failed_push_cannot_be_used_as_a_proof(repository: Path) -> None:
    lab = Lab(repository)
    git(repository, "remote", "set-url", "origin", str(repository.parent / "missing.git"))
    with pytest.raises(subprocess.CalledProcessError):
        plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, lab.ports())
    assert lab.pages == []


@pytest.mark.parametrize("failure,required", [("type-gate", "failure"), ("lint", "success")])
@pytest.mark.feature("lab-static-plant-proofs")
def test_a_wrong_red_gate_or_a_green_plant_is_refused(repository: Path, failure: str, required: str) -> None:
    lab = Lab(repository)
    lab.failure, lab.required = failure, required
    with pytest.raises(ValueError, match=r"\bplant\b"):
        plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, lab.ports())


@pytest.mark.parametrize("timeout_s", [0.5, 2, 900])
@pytest.mark.feature("lab-static-plant-proofs")
def test_an_incomplete_grade_expires_without_pushing_the_plant(repository: Path, timeout_s: float) -> None:
    lab = Lab(repository)
    lab.now = 10
    lab.incomplete = True

    def pending(path: str, remaining: float) -> Mapping[str, Any]:
        assert remaining == timeout_s
        rows = lab.fetch(path, remaining)
        lab.now += timeout_s - 0.5
        return rows

    ports = plant_grade.Ports(pending, lab.sleep, lab.clock, lab.push)
    with pytest.raises(TimeoutError, match=r"^grade .+ did not finish within .+ seconds$"):
        if timeout_s == 900:
            plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, ports)
        else:
            plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, ports, timeout_s=timeout_s)
    assert len(lab.heads) == 1
    assert lab.now == 10 + timeout_s


@pytest.mark.feature("lab-static-plant-proofs")
def test_an_unrelated_grade_expires_without_pushing_the_plant(repository: Path) -> None:
    lab = Lab(repository)
    lab.unrelated = True
    with pytest.raises(TimeoutError, match="grade"):
        plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, lab.ports(), timeout_s=2)
    assert len(lab.heads) == 1


@pytest.mark.feature("lab-static-plant-proofs")
def test_a_missing_required_check_keeps_the_grade_unfinished(repository: Path) -> None:
    lab = Lab(repository)

    def missing(path: str, timeout_s: float) -> Mapping[str, Any]:
        rows = lab.fetch(path)
        return {"check_runs": [row for row in rows["check_runs"] if row["name"] != "lab-shadow/required"]}

    with pytest.raises(TimeoutError, match="grade"):
        plant_grade.prove(
            repository,
            "lint",
            "HEAD",
            REPOSITORY,
            plant_grade.Ports(missing, lab.sleep, lab.clock, lab.push),
            timeout_s=2,
        )
    assert len(lab.heads) == 1


@pytest.mark.feature("lab-static-plant-proofs")
def test_two_required_checks_make_the_grade_ambiguous(repository: Path) -> None:
    lab = Lab(repository)

    def duplicate(path: str, timeout_s: float) -> Mapping[str, Any]:
        rows = list(lab.fetch(path)["check_runs"])
        required = next(
            row for row in rows if row["name"] == "lab-shadow/required" and row["external_id"] != "other-grade"
        )
        return {"check_runs": [*rows, {**required, "name": "lab/required"}]}

    with pytest.raises(ValueError, match=r"^grade grade-v1-[a-f0-9]+ has ambiguous required checks$"):
        plant_grade.prove(
            repository, "lint", "HEAD", REPOSITORY, plant_grade.Ports(duplicate, lab.sleep, lab.clock, lab.push)
        )
    assert len(lab.heads) == 1


@pytest.mark.feature("lab-static-plant-proofs")
def test_conflicting_gate_checks_cannot_hide_an_extra_failure(repository: Path) -> None:
    lab = Lab(repository)

    def conflict(path: str, timeout_s: float) -> Mapping[str, Any]:
        rows = list(lab.fetch(path)["check_runs"])
        head = path.split("/")[4]
        if git(repository, "show", f"{head}:sample.py") == "after":
            run = grade_key(GradeInputs(REPOSITORY, head, lab.base, "pull_request", lab.base))
            rows.extend(
                [
                    lab.check("lab-shadow/type-gate", "failure", run),
                    lab.check("lab/type-gate", "success", run),
                ]
            )
        return {"check_runs": rows}

    with pytest.raises(ValueError, match="ambiguous"):
        plant_grade.prove(
            repository, "lint", "HEAD", REPOSITORY, plant_grade.Ports(conflict, lab.sleep, lab.clock, lab.push)
        )


@pytest.mark.parametrize("name", ["lint", "gate-script"])
@pytest.mark.parametrize(
    "diagnostic,field",
    [
        (None, "text"),
        ("", "text"),
        ("F401 unused import", "text"),
        ("ruff: command not found", "text"),
        ("I0010", "text"),
        ("I001", "summary"),
        ("I001", "title"),
        ("forbidden comment", "summary"),
        ("forbidden comment", "title"),
    ],
)
@pytest.mark.feature("lab-static-plant-proofs")
def test_a_named_gate_failure_requires_the_observed_plant_diagnostic(
    repository: Path, name: str, diagnostic: str | None, field: str
) -> None:
    lab = Lab(repository, name)

    def wrong(path: str, timeout_s: float) -> Mapping[str, Any]:
        result = lab.fetch(path, timeout_s)
        for row in result["check_runs"]:
            if row["name"] == "lab-shadow/lint":
                if diagnostic is None:
                    row.pop("output", None)
                else:
                    row["output"] = {"title": "lint", "summary": "", "text": ""}
                    row["output"][field] = diagnostic
        return result

    with pytest.raises(ValueError, match="diagnostic"):
        plant_grade.prove(
            repository, name, "HEAD", REPOSITORY, plant_grade.Ports(wrong, lab.sleep, lab.clock, lab.push)
        )


@pytest.mark.feature("lab-static-plant-proofs")
def test_a_failed_twin_is_named_and_the_plant_is_never_pushed(repository: Path) -> None:
    lab = Lab(repository)
    lab.twin_required = "failure"
    with pytest.raises(ValueError, match=r"\btwin\b"):
        plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, lab.ports())
    assert len(lab.heads) == 1


@pytest.mark.feature("lab-static-plant-proofs")
def test_all_check_pages_are_read(repository: Path) -> None:
    lab = Lab(repository)
    lab.full_first_page = True
    row = plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, lab.ports())
    assert row["plant"] == "lint"
    assert sum(path.endswith("page=2") for path in lab.pages) == 2


@pytest.mark.feature("lab-static-plant-proofs")
def test_a_grade_arriving_at_its_deadline_is_refused(repository: Path) -> None:
    lab = Lab(repository)

    def late(path: str, timeout_s: float) -> Mapping[str, Any]:
        rows = lab.fetch(path)
        lab.now = 2
        return rows

    with pytest.raises(TimeoutError, match="grade"):
        plant_grade.prove(
            repository, "lint", "HEAD", REPOSITORY, plant_grade.Ports(late, lab.sleep, lab.clock, lab.push), timeout_s=2
        )
    assert len(lab.heads) == 1


@pytest.mark.feature("lab-static-plant-proofs")
def test_an_excessive_history_is_refused_after_one_hundred_pages(repository: Path) -> None:
    lab = Lab(repository)
    pages: list[str] = []

    def full(path: str, timeout_s: float) -> Mapping[str, Any]:
        pages.append(path)
        return {"check_runs": [lab.check("lab-shadow/lint", "failure", "other-grade")] * 100}

    with pytest.raises(ValueError, match="history"):
        plant_grade.prove(
            repository, "lint", "HEAD", REPOSITORY, plant_grade.Ports(full, lab.sleep, lab.clock, lab.push)
        )
    assert len(pages) == 100


@pytest.mark.parametrize("timeout_s", [0, -1])
@pytest.mark.feature("lab-static-plant-proofs")
def test_a_nonpositive_grade_deadline_is_refused_before_pushing(repository: Path, timeout_s: float) -> None:
    lab = Lab(repository)
    with pytest.raises(ValueError, match="^the grade timeout must be positive$"):
        plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, lab.ports(), timeout_s=timeout_s)
    assert git(repository, "ls-remote", "--heads", "origin") == ""


@pytest.mark.feature("lab-static-plant-proofs")
def test_a_completed_pair_can_be_proven_again_without_rewinding_its_remote(repository: Path) -> None:
    first, second = Lab(repository), Lab(repository)
    a = plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, first.ports())
    b = plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, second.ports())
    assert a == b
    assert first.heads == second.heads


@pytest.mark.feature("lab-static-plant-proofs")
def test_different_bases_use_independent_plant_branches(repository: Path) -> None:
    first = Lab(repository)
    a = plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, first.ports())
    git(repository, "commit", "--allow-empty", "-qm", "Advance protected base")
    second = Lab(repository)
    b = plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, second.ports())
    assert a["base"] != b["base"]
    tips = git(repository, "ls-remote", "--heads", "origin", "refs/heads/diffcheck/lab-plant-lint-*")
    assert len(tips.splitlines()) == 2
    assert {line.split()[0] for line in tips.splitlines()} == {first.heads[-1], second.heads[-1]}


@pytest.mark.feature("lab-static-plant-proofs")
def test_the_module_dispatch_exposes_the_plant_command(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit) as result:
        entrypoint.main(["grade", "--help"], {})
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert help_text.split(" [-h]", 1)[0] == "usage: python -m scripts.ci_lab grade"
    assert "--plant" in help_text
    for arguments in (["grade"], ["grade", "--plant", "other"]):
        with pytest.raises(SystemExit) as invalid:
            entrypoint.main(arguments, {})
        assert invalid.value.code == 2

    def command(argv: list[str], environ: Mapping[str, str]) -> int:
        assert argv == ["--plant", "lint"]
        assert environ == {"GITHUB_STEP_SUMMARY": "summary.md"}
        return 23

    monkeypatch.setattr(plant_grade, "main", command)
    assert entrypoint.main(["grade", "--plant", "lint"], {"GITHUB_STEP_SUMMARY": "summary.md"}) == 23


@pytest.mark.parametrize("name", ["lint", "gate-script"])
@pytest.mark.feature("lab-static-plant-proofs")
def test_the_command_prints_links_and_appends_the_proof_summary(
    repository: Path,
    tmp_path: Path,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repository)
    lab = Lab(repository, name)
    summary = tmp_path / "summary.md"
    summary.write_text("Earlier proof\n")
    assert (
        plant_grade.main(
            ["--plant", name, "--base", "HEAD", "--repository", REPOSITORY],
            {"GITHUB_STEP_SUMMARY": str(summary)},
            lab.ports(),
        )
        == 0
    )
    row = json.loads(capsys.readouterr().out)
    assert row["plant"] == name
    assert row["green"] == row["red"] == "https://github.com/acme/widgets/runs/required"
    text = summary.read_text()
    assert text.startswith("Earlier proof\n")
    assert name in text and ("I001" if name == "lint" else "forbidden comment") in text
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
    api = GitHub(repository)
    real_run = subprocess.run

    def transport(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if arguments[3:] == ["remote", "get-url", "origin"]:
            assert arguments[:3] == ["git", "-C", str(repository)]
            return subprocess.CompletedProcess(arguments, 0, "git@github.com:Acme/widgets.git\n", "")
        if Path(arguments[0]).name == "git":
            if arguments[3] == "ls-tree" and kwargs["timeout"] == 30:
                assert len(arguments) == 8 and arguments[4] == "-z" and arguments[6] == "--"
            if arguments[3:5] == ["cat-file", "blob"]:
                assert kwargs == {"capture_output": True, "check": True, "timeout": 30}
            return real_run(arguments, **kwargs)
        assert arguments[:2] == ["gh", "api"]
        path = arguments[2]
        if "/check-runs?" in path:
            assert arguments == ["gh", "api", path]
            assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 30}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(lab.fetch(path)), "")
        method = arguments[arguments.index("--method") + 1]
        body = json.loads(kwargs["input"]) if kwargs.get("input") is not None else None
        assert arguments == ["gh", "api", path, "--method", method, *(["--input", "-"] if body is not None else [])]
        assert kwargs == {
            "input": json.dumps(body) if body is not None else None,
            "capture_output": True,
            "text": True,
            "check": True,
            "timeout": 30,
        }
        result = api(method, path, body)
        if body and (path.endswith("/refs") or method == "PATCH"):
            branch = body["ref"] if method == "POST" else "refs/" + path.split("/git/refs/", 1)[1]
            real_run(
                ["git", "-C", str(repository), "push", "origin", f"{body['sha']}:{branch}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        return subprocess.CompletedProcess(arguments, 0, json.dumps(result), "")

    def no_http(*arguments: Any, **options: Any) -> None:
        raise AssertionError("the runner must not contact the lab receiver")

    lab.pending_first = True
    monkeypatch.setattr(plant_grade, "time", SimpleNamespace(sleep=lab.sleep, monotonic=lab.clock))
    monkeypatch.setattr(subprocess, "run", transport)
    monkeypatch.setattr(urllib.request, "urlopen", no_http)
    assert plant_grade.main(["--plant", "lint", "--base", "HEAD"], {}) == 0
    assert json.loads(capsys.readouterr().out)["plant"] == "lint"
    assert len(lab.heads) == 2
    assert lab.now == 10
    assert any(method == "PATCH" for method, _, _ in api.calls)


@pytest.mark.feature("lab-static-plant-proofs")
def test_the_default_fetch_respects_the_remaining_budget_on_rate_limits(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repository)
    lab = Lab(repository)
    real_run = subprocess.run
    timeouts: list[float] = []

    def limited(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if Path(arguments[0]).name == "git":
            return real_run(arguments, **kwargs)
        timeouts.append(kwargs["timeout"])
        raise subprocess.CalledProcessError(1, arguments, stderr="secondary rate limit exceeded")

    clock = SimpleNamespace(sleep=lab.sleep, monotonic=lab.clock)
    monkeypatch.setattr(plant_grade, "time", clock)
    monkeypatch.setattr(rate_limit, "time", clock)
    monkeypatch.setattr(rate_limit, "deadline", None)
    monkeypatch.setattr(rate_limit, "slept", 0)
    monkeypatch.setattr(plant_publish, "publish", lab.push)
    monkeypatch.setattr(subprocess, "run", limited)
    assert (
        plant_grade.main(
            ["--plant", "lint", "--base", "HEAD", "--repository", REPOSITORY, "--timeout", "2"],
            {},
        )
        == 1
    )
    assert capsys.readouterr().out == ""
    assert len(timeouts) == 1
    assert 0 < timeouts[0] <= 2
    assert lab.now <= 2


@pytest.mark.feature("lab-static-plant-proofs")
def test_a_refused_command_prints_no_success_row_or_summary(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repository)
    lab = Lab(repository)
    lab.required = "success"
    summary = tmp_path / "summary.md"
    assert (
        plant_grade.main(
            ["--plant", "lint", "--base", "HEAD", "--repository", REPOSITORY],
            {"GITHUB_STEP_SUMMARY": str(summary)},
            lab.ports(),
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "plant" in output.err
    assert not summary.exists()


@pytest.mark.feature("lab-static-plant-proofs")
def test_a_command_for_another_host_is_refused_before_pushing(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repository)
    lab = Lab(repository)
    assert (
        plant_grade.main(
            ["--plant", "lint", "--repository", "https://elsewhere.invalid/widgets"],
            {},
            lab.ports(),
        )
        == 1
    )
    answer = capsys.readouterr()
    assert answer.out == ""
    assert answer.err == "a GitHub repository URL is required\n"
    assert git(repository, "ls-remote", "--heads", "origin") == ""


@pytest.mark.parametrize(
    "remote,canonical",
    [
        ("git@github.com:Acme/widgetX", "https://github.com/acme/widgetx"),
        ("https://github.com/Acme/widgets.git/", REPOSITORY),
    ],
)
@pytest.mark.feature("lab-static-plant-proofs")
def test_the_command_defaults_and_remote_normalization(
    repository: Path,
    remote: str,
    canonical: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repository)
    git(repository, "remote", "set-url", "origin", remote)

    def proof(
        root: Path,
        name: str,
        base: str,
        normalized: str,
        ports: plant_grade.Ports,
        timeout_s: float,
    ) -> dict[str, str]:
        assert root == repository
        assert name == "lint"
        assert base == "origin/dev"
        assert normalized == canonical
        assert timeout_s == 900
        return {"plant": name}

    monkeypatch.setattr(plant_grade, "prove", proof)
    assert plant_grade.main(["--plant", "lint"], {}) == 0
    assert json.loads(capsys.readouterr().out) == {"plant": "lint"}


@pytest.mark.parametrize("planted,missing,extra", [(False, True, False), (False, False, True), (True, False, True)])
@pytest.mark.feature("lab-static-plant-proofs")
def test_a_grade_requires_its_named_gate_and_no_other_failure(
    repository: Path, planted: bool, missing: bool, extra: bool
) -> None:
    lab = Lab(repository)

    def checks(path: str, timeout_s: float) -> Mapping[str, Any]:
        result = lab.fetch(path, timeout_s)
        head = path.split("/")[4]
        red = git(repository, "show", f"{head}:sample.py") == "after"
        if red == planted:
            run = grade_key(GradeInputs(REPOSITORY, head, lab.base, "pull_request", lab.base))
            if missing:
                result["check_runs"][:] = [row for row in result["check_runs"] if row["name"] != "lab-shadow/lint"]
            if extra:
                result["check_runs"].append(lab.check("lab-shadow/type-gate", "failure", run))
        return result

    with pytest.raises(ValueError, match="wrong verdict"):
        plant_grade.prove(
            repository, "lint", "HEAD", REPOSITORY, plant_grade.Ports(checks, lab.sleep, lab.clock, lab.push)
        )


@pytest.mark.feature("lab-static-plant-proofs")
def test_a_check_request_never_starts_at_its_deadline(repository: Path) -> None:
    lab = Lab(repository)
    lab.now = 10

    def forbidden(path: str, timeout_s: float) -> Mapping[str, Any]:
        raise AssertionError("an expired grade must not issue another request")

    with pytest.raises(TimeoutError, match="^grade current expired while reading checks$"):
        plant_grade._matching(
            REPOSITORY, lab.base, "current", plant_grade.Ports(forbidden, lab.sleep, lab.clock, lab.push), 10
        )


@pytest.mark.feature("lab-static-plant-proofs")
def test_proof_links_distinguish_the_twin_and_plant_and_accept_a_named_diagnostic(repository: Path) -> None:
    catalog = repository / "tests/ci_lab/plants/lint.json"
    definition = json.loads(catalog.read_text())
    definition["reason"] = "I001: import block order"
    catalog.write_text(json.dumps(definition))
    git(repository, "add", str(catalog))
    git(repository, "commit", "-qm", "Describe the diagnostic")
    lab = Lab(repository)

    def checks(path: str, timeout_s: float) -> Mapping[str, Any]:
        result = lab.fetch(path, timeout_s)
        head = path.split("/")[4]
        for row in result["check_runs"]:
            row["html_url"] += "/" + head
        return result

    result = plant_grade.prove(
        repository, "lint", "HEAD", REPOSITORY, plant_grade.Ports(checks, lab.sleep, lab.clock, lab.push)
    )
    assert result["green"] == f"https://github.com/acme/widgets/runs/required/{lab.heads[0]}"
    assert result["red"] == f"https://github.com/acme/widgets/runs/required/{lab.heads[1]}"
    assert result["reason"] == "I001: import block order"


@pytest.mark.feature("lab-static-plant-proofs")
def test_the_command_accepts_a_discovered_catalog_plant(
    repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    name = "lint.lint-check"
    catalog = repository / "tests/ci_lab/plants"
    definition = json.loads((catalog / "gate-script.json").read_text())
    (catalog / f"{name}.json").write_text(json.dumps(definition))
    lab = Lab(repository, name)
    monkeypatch.chdir(repository)
    assert plant_grade.main(["--plant", name, "--base", "HEAD", "--repository", REPOSITORY], {}, lab.ports()) == 0
    assert json.loads(capsys.readouterr().out)["plant"] == name


@pytest.mark.parametrize("diagnostic", ["I001", "missing-diagnostic"])
@pytest.mark.feature("lab-static-plant-proofs")
def test_a_step_plant_uses_its_job_check_and_requires_its_diagnostic(repository: Path, diagnostic: str) -> None:
    catalog = repository / "tests/ci_lab/plants/lint.json"
    definition = json.loads(catalog.read_text())
    definition["gate"] = "semgrep/scan/rule-exceptions-only-shrink"
    definition["reason"] = f"{diagnostic}: the step diagnostic"
    catalog.write_text(json.dumps(definition))
    lab = Lab(repository)

    def fetch(path: str, timeout_s: float) -> Mapping[str, Any]:
        result = lab.fetch(path, timeout_s)
        for row in result["check_runs"]:
            if row["name"] == "lab-shadow/lint":
                row["name"] = "lab-shadow/semgrep"
        return result

    ports = plant_grade.Ports(fetch, lab.sleep, lab.clock, lab.push)
    if diagnostic == "missing-diagnostic":
        with pytest.raises(ValueError, match="plant grade lacks the expected diagnostic"):
            plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, ports)
    else:
        result = plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, ports)
        assert result["gate"] == definition["gate"]
        assert len(lab.heads) == 2


@pytest.mark.feature("lab-static-plant-proofs")
def test_a_plant_proof_reports_the_green_and_red_grade_run_ids(repository: Path) -> None:
    lab = Lab(repository)
    result = plant_grade.prove(repository, "lint", "HEAD", REPOSITORY, lab.ports())
    green, red = lab.heads
    assert result["green_run"] == grade_key(GradeInputs(REPOSITORY, green, lab.base, "pull_request", lab.base))
    assert result["red_run"] == grade_key(GradeInputs(REPOSITORY, red, lab.base, "pull_request", lab.base))
    assert result["green_run"] != result["red_run"]
