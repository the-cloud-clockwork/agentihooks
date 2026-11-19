import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.ci_lab.remote import checkout

pytestmark = pytest.mark.unit

LAB = "http://10.10.30.208/agentihooks.git"
URL_ENV = "AGENTIHOOKS_LAB_REMOTE"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path: Path, origin: str) -> Path:
    repo = tmp_path / "checkout"
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "remote", "add", "origin", origin)
    return repo


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("git@github.com:The-Cloud-Clockwork/AgentiHooks.git", "agentihooks"),
        ("https://github.com/the-cloud-clockwork/agentihooks", "agentihooks"),
        ("http://10.10.30.208/agentihooks.git/\n", "agentihooks"),
        ("http://lab.example/TEAMX/", "teamx"),
        ("/srv/git/origin.git", "origin"),
        ("", ""),
    ],
)
@pytest.mark.feature("lab-remote-in-workspaces")
def test_name_is_the_lowercase_repository_a_remote_ends_in(url: str, name: str) -> None:
    assert checkout.name(url) == name


@pytest.mark.parametrize(
    ("lab", "origin", "served"),
    [
        (LAB, "git@github.com:the-cloud-clockwork/agentihooks.git", True),
        (LAB, "git@github.com:the-cloud-clockwork/antoncore.git", False),
        ("", "git@github.com:the-cloud-clockwork/agentihooks.git", False),
        ("", "", False),
        (".git", "", False),
    ],
)
@pytest.mark.feature("lab-remote-in-workspaces")
def test_serves_only_the_repository_its_origin_names(lab: str, origin: str, served: bool) -> None:
    assert checkout.serves(lab, origin) is served


@pytest.mark.feature("lab-remote-in-workspaces")
def test_carry_adds_the_lab_remote_and_a_token_helper_that_stores_no_token(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "git@github.com:the-cloud-clockwork/agentihooks.git")
    assert checkout.carry(checkout.at(repo), {URL_ENV: LAB}) is True
    assert _git(repo, "remote", "get-url", "lab") == LAB
    assert _git(repo, "config", "--get", f"credential.{LAB}.helper") == checkout.HELPER
    assert "s3cret" not in (repo / ".git" / "config").read_text()
    fill = subprocess.run(
        [str(shutil.which("git")), "-C", str(repo), "credential", "fill"],
        input=f"url={LAB}\n\n",
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "AGENTIHOOKS_LAB_REMOTE_TOKEN": "s3cret",
            "GIT_TERMINAL_PROMPT": "0",
        },
        check=True,
    )
    assert {"username=lab", "password=s3cret"} <= set(fill.stdout.splitlines())


@pytest.mark.feature("lab-remote-in-workspaces")
def test_carry_points_an_existing_lab_remote_at_the_lab_url(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "git@github.com:the-cloud-clockwork/agentihooks.git")
    _git(repo, "remote", "add", "lab", "http://old.example/agentihooks.git")
    assert checkout.carry(checkout.at(repo), {URL_ENV: LAB}) is True
    assert _git(repo, "remote", "get-url", "lab") == LAB


@pytest.mark.parametrize("environ", [{}, {URL_ENV: ""}, {URL_ENV: "http://10.10.30.208/antoncore.git"}])
@pytest.mark.feature("lab-remote-in-workspaces")
def test_carry_leaves_a_repository_the_lab_does_not_serve_untouched(tmp_path: Path, environ: dict[str, str]) -> None:
    repo = _repo(tmp_path, "git@github.com:the-cloud-clockwork/agentihooks.git")
    before = (repo / ".git" / "config").read_text()
    assert checkout.carry(checkout.at(repo), environ) is False
    assert (repo / ".git" / "config").read_text() == before


@pytest.mark.feature("lab-remote-in-workspaces")
def test_carry_without_a_lab_url_leaves_even_a_repository_named_like_a_placeholder_untouched(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "git@github.com:the-cloud-clockwork/XXXX.git")
    assert checkout.carry(checkout.at(repo), {}) is False
    assert _git(repo, "remote") == "origin"


@pytest.mark.feature("lab-remote-in-workspaces")
def test_at_runs_git_in_the_checkout_with_its_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(checkout.subprocess, "run", lambda argv, **options: calls.append((argv, options)))
    checkout.at(tmp_path)("status")
    assert calls == [(["git", "-C", str(tmp_path), "status"], {"capture_output": True, "text": True, "timeout": 60})]


@pytest.mark.parametrize(
    ("args", "branches"),
    [
        (["-o", "branch=engineer-1", "lab", "HEAD:refs/grade/t1"], ["engineer-1"]),
        (["lab", "-obranch=engineer-1", "HEAD:refs/grade/t1"], ["engineer-1"]),
        (["--push-option", "branch=engineer-1", "lab", "HEAD:refs/grade/t1"], ["engineer-1"]),
        (["--push-option=branch=engineer-1", "lab", "HEAD:refs/grade/t1"], ["engineer-1"]),
        (["-o", "base=abc", "lab", "HEAD:refs/grade/t1"], []),
        (["-o", "branch=", "lab", "HEAD:refs/grade/t1"], []),
        (["lab", "HEAD:refs/grade/branch=x"], []),
    ],
)
@pytest.mark.feature("lab-remote-in-workspaces")
def test_relayed_reads_the_branch_push_options_of_a_lab_push(args: list[str], branches: list[str]) -> None:
    assert checkout.relayed(args) == branches
