import importlib.util
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from scripts import no_comments
from scripts.ci_lab import __main__ as lab_cli
from scripts.ci_lab import plant
from scripts.ci_lab.digests import GradeInputs, grade_key
from scripts.hub.github_webhooks import verify

ROOT = Path(__file__).resolve().parents[3]
SECRET = "notice secret"
SLUG = "owner/name"
REPOSITORY = "https://github.com/owner/name"
ACTOR = "a-trusted-login"
NOTICE_URL = "http://agentihooks-swarm-github-webhook.agentihooks-swarm.svc:8790/lab/head"
BRANCH = "refs/heads/diffcheck/ci-lab-swap"
ZERO = "0" * 40
BEFORE = "import os\nimport sys\n"
AFTER = "import sys\nimport os\n"
IDENTITY = "agentihooks lab plant lab-plant@agentihooks.invalid"
GREEN = {"required": "success", "lint": "success", "type-gate": "success"}
RED = {"required": "failure", "lint": "failure", "type-gate": "success"}
ENVIRON = {
    "GITHUB_REPOSITORY": SLUG,
    "GITHUB_ACTOR": ACTOR,
    "AGENTIHOOKS_CI_LAB_NOTICE_SECRET": SECRET,
    "GITHUB_TOKEN": "token",
}


def _git(root: Path, *argv: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _commit(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(root, "-c", "user.name=base", "-c", "user.email=base@example.invalid", "commit", "-q", "-m", message)


def _run(name: str, external_id: str, conclusion: str | None) -> dict[str, Any]:
    return {
        "name": name,
        "external_id": external_id,
        "status": "in_progress" if conclusion is None else "completed",
        "conclusion": conclusion,
        "html_url": f"https://github.com/{SLUG}/runs/{external_id}/{name}",
    }


def _key(head: str, base: str) -> str:
    return grade_key(GradeInputs(REPOSITORY, head, base, "pull_request", base))


def _checks(conclusions: Mapping[str, str]) -> dict[str, plant.Check]:
    return {gate: plant.Check("completed", conclusion, f"url/{gate}") for gate, conclusion in conclusions.items()}


def _pushes(tree: Path) -> list[str]:
    return [line for line in (tree.parent / "pushes.log").read_text().splitlines() if "diffcheck" in line]


def _module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ruff(text: str, path: str) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "ruff", "check", "--isolated", "--select", "I", "--stdin-filename", path, "-"]
    return subprocess.run(argv, input=text, capture_output=True, text=True, cwd=ROOT)


def _never(*_: object) -> Any:
    raise AssertionError("not called")


def _settings(**changes: Any) -> plant.Settings:
    values: dict[str, Any] = {
        "repository": SLUG,
        "actor": ACTOR,
        "secret": SECRET.encode(),
        "base": "dev",
        "remote": "origin",
        "notice_url": "http://lab/notice",
        "wait_s": 60.0,
        "poll_s": 20.0,
    }
    return plant.Settings(**{**values, **changes})


class Lab:
    """The lab and GitHub as a proof sees them: notices are recorded, and each noticed head's check runs answer in
    progress on its first reads and completed with its role's conclusions after."""

    def __init__(self, twin: Mapping[str, str], head: Mapping[str, str], status: int = 200, pending: int = 1) -> None:
        self.conclusions = (twin, head)
        self.status = status
        self.pending = pending
        self.notices: list[tuple[str, bytes, dict[str, str]]] = []
        self.reads: list[str] = []
        self.sleeps: list[float] = []

    def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> int:
        self.notices.append((url, body, dict(headers)))
        return self.status

    def get(self, path: str) -> dict[str, Any]:
        self.reads.append(path)
        listing, query = path.split("?")
        head = listing.removeprefix(f"repos/{SLUG}/commits/").removesuffix("/check-runs")
        assert query == "filter=all&per_page=100&page=1"
        noticed = [json.loads(body) for _, body, _ in self.notices]
        role = [item["head"] for item in noticed].index(head)
        run = _key(head, noticed[role]["base"])
        done = sum(1 for read in self.reads if f"/{head}/" in read) > self.pending
        runs = [
            _run(f"lab-shadow/{gate}", run, conclusion if done else None)
            for gate, conclusion in self.conclusions[role].items()
        ]
        decoys = [_run("lab-shadow/lint", "grade-v1-other", "success"), _run("lint", run, "failure")]
        return {"total_count": len(decoys) + len(runs), "check_runs": decoys + runs}

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def ports(self) -> plant.Ports:
        return plant.Ports(self.post, self.get, self.sleep)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    remote, root = tmp_path / "remote.git", tmp_path / "tree"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    hook = remote / "hooks" / "post-receive"
    hook.write_text(f'#!/bin/sh\ncat >> "{tmp_path / "pushes.log"}"\n')
    hook.chmod(0o755)
    subprocess.run(["git", "init", "-q", "-b", "dev", str(root)], check=True)
    (root / "units.py").write_text(f"{BEFORE}\nVALUE = 1\n")
    (root / "gate.py").write_text("RULES = ()\n")
    (root / plant.PLANTS).mkdir(parents=True)
    edits = [
        {"path": "units.py", "before": BEFORE, "after": AFTER},
        {"path": "units.py", "before": "VALUE = 1\n", "after": "VALUE = 2\n"},
        {"path": "gate.py", "before": "RULES = ()\n", "after": "RULES = (1,)\n"},
    ]
    (root / plant.PLANTS / "swap.json").write_text(json.dumps({"gate": "lint", "reason": "unsorted", "edits": edits}))
    _commit(root, "base")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-q", "origin", "dev")
    return root


@pytest.mark.feature("lab-plant-proofs")
def test_every_plant_replaces_text_found_once_in_this_tree() -> None:
    names = sorted(path.stem for path in (ROOT / plant.PLANTS).glob("*.json"))
    assert names == ["gate-script", "lint"]
    for name in names:
        found = plant.load(ROOT, name)
        assert (found.name, found.gate) == (name, "lint")
        for edit in found.edits:
            assert (ROOT / edit.path).read_text().count(edit.before) == 1, f"{name} {edit.path}"


@pytest.mark.feature("lab-plant-proofs")
def test_the_lint_plant_is_an_import_block_ruff_refuses_as_unsorted() -> None:
    (edit,) = plant.load(ROOT, "lint").edits
    text = (ROOT / edit.path).read_text()
    assert _ruff(text, edit.path).returncode == 0
    refused = _ruff(plant.planted(text, edit), edit.path)
    assert refused.returncode == 1
    assert "I001" in refused.stdout


@pytest.mark.feature("lab-plant-proofs")
def test_the_gate_script_plant_passes_only_the_heads_edited_comment_gate(tmp_path: Path) -> None:
    script, comment = plant.load(ROOT, "gate-script").edits
    edited = tmp_path / "edited_no_comments.py"
    edited.write_text(plant.planted((ROOT / script.path).read_text(), script))
    text = (ROOT / comment.path).read_text()
    source = plant.planted(text, comment)
    assert no_comments.violations(text, comment.path) == []
    assert [reason for *_, reason in no_comments.violations(source, comment.path)] == ["forbidden comment"]
    assert _module(edited).violations(source, comment.path) == []


@pytest.mark.feature("lab-plant-proofs")
def test_grade_cuts_a_twin_with_the_base_tree_and_the_plant_on_top(tree: Path) -> None:
    lab = Lab(GREEN, RED)
    line, found = plant.prove(tree, plant.load(tree, "swap"), _settings(), lab.ports())
    base = _git(tree, "rev-parse", "dev")
    head = _git(tree, "ls-remote", "origin", BRANCH).split()[0]
    twin = _git(tree, "rev-parse", f"{head}^")
    assert found == []
    assert _git(tree, "rev-parse", f"{twin}^") == base
    assert _git(tree, "rev-parse", f"{twin}^{{tree}}") == _git(tree, "rev-parse", f"{base}^{{tree}}")
    assert _git(tree, "show", f"{head}:units.py") == "import sys\nimport os\n\nVALUE = 2"
    assert _git(tree, "show", f"{head}:gate.py") == "RULES = (1,)"
    assert _git(tree, "diff", "--name-only", twin, head).splitlines() == ["gate.py", "units.py"]
    assert _git(tree, "log", "-2", "--format=%s", head).splitlines() == [
        "Lab plant swap: unsorted",
        "Lab plant swap: the base tree",
    ]
    when = _git(tree, "log", "-1", "--format=%ct", base)
    stamp = f"{IDENTITY}|{IDENTITY}|{when} +0000"
    assert _git(tree, "log", "-2", "--format=%an %ae|%cn %ce|%ct %cz", head).splitlines() == [stamp, stamp]
    links = [f"https://github.com/{SLUG}/runs/{_key(sha, base)}/lab-shadow/lint" for sha in (twin, head)]
    assert line == (
        f"| `swap` | `lint` | unsorted | [success]({links[0]}) on `{twin[:12]}` | "
        f"[failure]({links[1]}) on `{head[:12]}` |"
    )


@pytest.mark.feature("lab-plant-proofs")
def test_grade_sends_a_signed_notice_for_the_twin_and_the_plant_against_the_base(tree: Path) -> None:
    lab = Lab(GREEN, RED)
    plant.prove(tree, plant.load(tree, "swap"), _settings(), lab.ports())
    base = _git(tree, "rev-parse", "dev")
    head = _git(tree, "ls-remote", "origin", BRANCH).split()[0]
    heads = [_git(tree, "rev-parse", f"{head}^"), head]
    assert [url for url, _, _ in lab.notices] == ["http://lab/notice", "http://lab/notice"]
    assert [json.loads(body) for _, body, _ in lab.notices] == [
        {"repo": REPOSITORY, "head": sha, "base": base, "actor": ACTOR} for sha in heads
    ]
    for _, body, headers in lab.notices:
        assert sorted(headers) == ["Content-Type", "X-Hub-Signature-256"]
        assert headers["Content-Type"] == "application/json"
        assert verify(SECRET.encode(), body, headers["X-Hub-Signature-256"])
    assert lab.sleeps == [20.0, 20.0]
    assert _pushes(tree) == [f"{ZERO} {head} {BRANCH}"]


@pytest.mark.feature("lab-plant-proofs")
def test_a_rerun_on_the_same_base_cuts_the_same_commits_and_pushes_nothing(tree: Path) -> None:
    swap = plant.load(tree, "swap")
    first = plant.commits(tree, swap, "dev")
    assert plant.push(tree, "origin", first, "swap") == "diffcheck/ci-lab-swap"
    assert plant.commits(tree, swap, "dev") == first
    assert plant.push(tree, "origin", first, "swap") == "diffcheck/ci-lab-swap"
    assert _pushes(tree) == [f"{ZERO} {first.plant} {BRANCH}"]


@pytest.mark.feature("lab-plant-proofs")
def test_a_plant_cut_on_a_new_base_deletes_the_old_branch_before_its_push(tree: Path) -> None:
    swap = plant.load(tree, "swap")
    first = plant.commits(tree, swap, "dev")
    plant.push(tree, "origin", first, "swap")
    (tree / "other.py").write_text("OTHER = 1\n")
    _commit(tree, "next")
    second = plant.commits(tree, swap, "dev")
    plant.push(tree, "origin", second, "swap")
    assert second.base == _git(tree, "rev-parse", "dev") != first.base
    assert _pushes(tree) == [
        f"{ZERO} {first.plant} {BRANCH}",
        f"{first.plant} {ZERO} {BRANCH}",
        f"{ZERO} {second.plant} {BRANCH}",
    ]


@pytest.mark.feature("lab-plant-proofs")
@pytest.mark.parametrize(("text", "found"), [("", 0), (BEFORE + BEFORE, 2)])
def test_a_plant_text_held_other_than_once_is_refused(text: str, found: int) -> None:
    with pytest.raises(plant.ProofError) as refused:
        plant.planted(text, plant.Edit("units.py", BEFORE, AFTER))
    assert str(refused.value) == f"units.py holds the plant's text {found} times, not once"


@pytest.mark.feature("lab-plant-proofs")
@pytest.mark.parametrize(
    ("twin", "head", "expected"),
    [
        (GREEN, RED, []),
        ({**GREEN, "required": "failure"}, RED, ["the twin's required check concluded failure, not success"]),
        (GREEN, {**RED, "required": "success"}, ["the plant's required check concluded success, not failure"]),
        (GREEN, {**RED, "type-gate": "failure"}, ["the plant failed lint, type-gate, not lint alone"]),
        (GREEN, {**RED, "lint": "success"}, ["the plant failed no gate, not lint alone"]),
        (GREEN, {**RED, "lint": "success", "type-gate": "cancelled"}, ["the plant failed type-gate, not lint alone"]),
    ],
)
def test_the_proof_holds_only_when_the_twin_passes_and_the_plant_fails_its_gate_alone(
    twin: Mapping[str, str], head: Mapping[str, str], expected: list[str]
) -> None:
    swap = plant.Plant("swap", "lint", "unsorted", ())
    assert plant.problems(swap, _checks(twin), _checks(head)) == expected


@pytest.mark.feature("lab-plant-proofs")
def test_checks_read_every_page_and_keep_only_the_grades_lab_check_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plant, "PAGE", 2)
    pages = {
        1: [_run("lab-shadow/lint", "run", "failure"), _run("lab/lint", "other", "success")],
        2: [_run("lab/required", "run", "failure")],
    }
    paths: list[str] = []

    def get(path: str) -> dict[str, Any]:
        paths.append(path)
        return {"check_runs": pages[int(path.rpartition("=")[2])]}

    found = plant.checks(plant.Ports(_never, get, _never), SLUG, "abc", "run")
    assert paths == [f"repos/{SLUG}/commits/abc/check-runs?filter=all&per_page=2&page={page}" for page in (1, 2)]
    assert found == {
        "lint": plant.Check("completed", "failure", f"https://github.com/{SLUG}/runs/run/lab-shadow/lint"),
        "required": plant.Check("completed", "failure", f"https://github.com/{SLUG}/runs/run/lab/required"),
    }


@pytest.mark.feature("lab-plant-proofs")
def test_a_grade_the_lab_never_completes_fails_after_its_last_poll() -> None:
    reads: list[str] = []
    sleeps: list[float] = []

    def get(path: str) -> dict[str, Any]:
        reads.append(path)
        return {"check_runs": [_run("lab-shadow/required", "run", None)]}

    with pytest.raises(plant.ProofError) as refused:
        plant.settled(plant.Ports(_never, get, sleeps.append), SLUG, "abc", "run", 50.0, 20.0)
    assert str(refused.value) == "the lab completed no required check of grade run on abc within 50 seconds"
    assert (len(reads), sleeps) == (4, [20.0, 20.0, 20.0])


@pytest.mark.feature("lab-plant-proofs")
def test_a_refused_notice_stops_the_proof(tree: Path) -> None:
    swap = plant.load(tree, "swap")
    lab = Lab(GREEN, RED, status=401)
    with pytest.raises(plant.ProofError) as refused:
        plant.prove(tree, swap, _settings(), lab.ports())
    assert str(refused.value) == f"the lab notice of {plant.commits(tree, swap, 'dev').twin} answered 401"
    assert (len(lab.notices), lab.reads) == (1, [])


@pytest.mark.feature("lab-plant-proofs")
def test_grade_prints_the_proof_table_and_exits_by_the_proof(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tree)
    _git(tree, "fetch", "-q", "origin")
    held = Lab(GREEN, RED)
    assert plant.main(["--plant", "swap", "--wait", "60"], ENVIRON, held.ports()) == 0
    printed = capsys.readouterr()
    lines = printed.out.splitlines()
    assert lines[:2] == ["| Plant | Gate | Reason | Without the plant | With the plant |", "|---|---|---|---|---|"]
    assert lines[2].startswith("| `swap` | `lint` | unsorted | [success](")
    assert (len(lines), printed.err) == (3, "")
    assert [url for url, _, _ in held.notices] == [NOTICE_URL, NOTICE_URL]
    assert [json.loads(body)["actor"] for _, body, _ in held.notices] == [ACTOR, ACTOR]
    environ = {**ENVIRON, "GITHUB_TRIGGERING_ACTOR": "dispatcher"}
    failed = Lab(GREEN, GREEN)
    assert plant.main(["--plant", "swap", "--wait", "60"], environ, failed.ports()) == 1
    assert capsys.readouterr().err == (
        "grade: the plant's required check concluded success, not failure\n"
        "grade: the plant failed no gate, not lint alone\n"
    )
    assert [json.loads(body)["actor"] for _, body, _ in failed.notices] == ["dispatcher", "dispatcher"]


@pytest.mark.feature("lab-plant-proofs")
def test_grade_waits_thirty_minutes_polling_every_twenty_seconds_by_default(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tree)
    _git(tree, "fetch", "-q", "origin")
    lab = Lab(GREEN, RED, pending=1000)
    assert plant.main(["--plant", "swap"], ENVIRON, lab.ports()) == 2
    assert lab.sleeps == [20.0] * 90
    assert capsys.readouterr().err.startswith("grade: the lab completed no required check of grade grade-v1-")


@pytest.mark.feature("lab-plant-proofs")
def test_grade_refuses_to_run_without_its_repository_actor_secret_and_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert lab_cli.main(["grade", "--plant", "lint"], {}) == 2
    assert capsys.readouterr().err == (
        "grade: set --repository, --actor, AGENTIHOOKS_CI_LAB_NOTICE_SECRET, GITHUB_TOKEN\n"
    )


@pytest.mark.feature("lab-plant-proofs")
def test_the_real_ports_post_the_notice_and_read_github_with_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    class Answer:
        status_code = 202

        def raise_for_status(self) -> None:
            calls.append(("raise", "", {}))

        def json(self) -> dict[str, Any]:
            return {"check_runs": []}

    def post(url: str, **options: Any) -> Answer:
        calls.append(("post", url, options))
        return Answer()

    def get(url: str, **options: Any) -> Answer:
        calls.append(("get", url, options))
        return Answer()

    monkeypatch.setattr(plant.requests, "post", post)
    monkeypatch.setattr(plant.requests, "get", get)
    assert plant._post("http://lab/notice", b"{}", {"X-Hub-Signature-256": "sha256=x"}) == 202
    assert plant._get("token", f"repos/{SLUG}/commits/abc/check-runs") == {"check_runs": []}
    headers = {
        "Authorization": "Bearer token",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    assert calls == [
        ("post", "http://lab/notice", {"data": b"{}", "headers": {"X-Hub-Signature-256": "sha256=x"}, "timeout": 30}),
        ("get", f"https://api.github.com/repos/{SLUG}/commits/abc/check-runs", {"headers": headers, "timeout": 30}),
        ("raise", "", {}),
    ]
