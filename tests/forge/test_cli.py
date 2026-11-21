import io
import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from scripts import cli_delegates
from scripts.forge import cli, remote
from scripts.forge.model import ChangeRequest, Check

pytestmark = pytest.mark.unit
URL = "git@github.com:o/r.git"
CHANGE = ChangeRequest(
    number=7,
    url="https://github.com/o/r/pull/7",
    title="Add the example",
    body="Reviewer seats read the change.",
    head="abc123",
    diff="diff --git a/a.py b/a.py\n",
    files=("a.py",),
    checks=(Check("lint", "SUCCESS"),),
)
READ = {
    "number": 7,
    "url": "https://github.com/o/r/pull/7",
    "title": "Add the example",
    "body": "Reviewer seats read the change.",
    "head": "abc123",
    "diff": "diff --git a/a.py b/a.py\n",
    "files": ["a.py"],
    "checks": [{"name": "lint", "state": "SUCCESS"}],
}
HELP = """usage: agentihooks forge [-h] {read,post} ...

Read a change request or post a review

positional arguments:
  {read,post}
    read       print the change request as JSON
    post       post a review comment and print its URL

options:
  -h, --help   show this help message and exit
"""
READ_HELP = """usage: agentihooks forge read [-h] [--remote REMOTE] [--forge {github,gitlab}] number

positional arguments:
  number

options:
  -h, --help            show this help message and exit
  --remote REMOTE       remote URL, default the origin of the current checkout
  --forge {github,gitlab}
                        forge kind for a host that does not name one
"""
POST_HELP = """usage: agentihooks forge post [-h] --body-file BODY_FILE [--remote REMOTE]
                              [--forge {github,gitlab}]
                              number

positional arguments:
  number

options:
  -h, --help            show this help message and exit
  --body-file BODY_FILE
                        file holding the comment, - for standard input
  --remote REMOTE       remote URL, default the origin of the current checkout
  --forge {github,gitlab}
                        forge kind for a host that does not name one
"""


class Picked:
    """A fake picker that records how it was called and hands back itself as the forge."""

    def __init__(self) -> None:
        self.calls: list[tuple[remote.Remote, str | None, Mapping[str, str]]] = []
        self.reads: list[int] = []
        self.posts: list[tuple[int, str]] = []

    def __call__(self, where: remote.Remote, kind: str | None, env: Mapping[str, str]) -> "Picked":
        self.calls.append((where, kind, env))
        return self

    def read(self, number: int) -> ChangeRequest:
        self.reads.append(number)
        return CHANGE

    def post(self, number: int, body: str) -> str:
        self.posts.append((number, body))
        return "https://github.com/o/r/pull/7#issuecomment-1"


@pytest.fixture
def picked(monkeypatch: pytest.MonkeyPatch) -> Picked:
    fake = Picked()
    monkeypatch.setattr(remote, "pick", fake)
    return fake


@pytest.mark.feature("forge-change-request-seam")
def test_the_cli_delegates_forge_to_this_module() -> None:
    assert cli_delegates.DELEGATED_CLIS["forge"] == "scripts.forge.cli"
    assert cli_delegates.delegated_cli(["forge", "read"]) is cli.main


@pytest.mark.feature("forge-change-request-seam")
def test_read_prints_the_change_request_as_json(picked: Picked, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["read", "7", "--remote", URL]) == 0
    assert capsys.readouterr().out == json.dumps(READ, indent=2) + "\n"
    assert picked.calls == [(remote.Remote("github.com", "o/r"), None, os.environ)]
    assert picked.reads == [7]


@pytest.mark.feature("forge-change-request-seam")
@pytest.mark.parametrize(
    ("argv", "text"), [(["--help"], HELP), (["read", "--help"], READ_HELP), (["post", "--help"], POST_HELP)]
)
def test_the_forge_help_names_every_command_and_option(
    argv: list[str], text: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    with pytest.raises(SystemExit) as exited:
        cli.parse(argv)
    assert (exited.value.code, capsys.readouterr().out) == (0, text)


@pytest.mark.feature("forge-change-request-seam")
@pytest.mark.parametrize("argv", [[], ["post", "7", "--remote", URL]])
def test_a_missing_command_or_body_file_is_a_usage_error(argv: list[str], picked: Picked) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(argv)
    assert exited.value.code == 2
    assert picked.calls == []


@pytest.mark.feature("forge-change-request-seam")
def test_post_reads_the_body_file_and_prints_the_comment_url(
    picked: Picked, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = tmp_path / "review.md"
    body.write_text("Finding one\n")
    assert cli.main(["post", "7", "--body-file", str(body), "--remote", URL, "--forge", "github"]) == 0
    assert capsys.readouterr().out == "https://github.com/o/r/pull/7#issuecomment-1\n"
    assert picked.posts == [(7, "Finding one\n")]
    assert picked.calls[0][1] == "github"


@pytest.mark.feature("forge-change-request-seam")
def test_post_reads_the_body_from_standard_input(picked: Picked, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("From standard input"))
    assert cli.main(["post", "7", "--body-file", "-", "--remote", URL]) == 0
    assert picked.posts == [(7, "From standard input")]


@pytest.mark.feature("forge-change-request-seam")
def test_the_origin_remote_of_the_current_checkout_is_the_default(
    picked: Picked, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(remote, "origin", lambda directory: asked.append(directory) or URL)
    assert cli.main(["read", "7"]) == 0
    assert asked == ["."]
    assert picked.calls[0][0] == remote.Remote("github.com", "o/r")


@pytest.mark.feature("forge-change-request-seam")
def test_a_forge_failure_prints_its_reason_and_returns_one(picked: Picked, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["read", "7", "--remote", "/local/repo"]) == 1
    assert capsys.readouterr() == ("", "forge: not a repository remote: /local/repo\n")
    assert picked.calls == []
