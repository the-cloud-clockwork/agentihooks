import json
import os
import shlex
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import cast

import pytest

from scripts.ci_lab import intake
from scripts.ci_lab.remote import post_receive
from scripts.ci_lab.scheduler import DURABLE, SUBJECT, BusInbox
from scripts.ci_lab.units import Priority
from scripts.events.bus import Bus, Consumer
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus

ROOT = Path(__file__).resolve().parents[2]
REPO = "https://github.com/acme/widgets"
ACTOR = "agenticore-app[bot]"
LAB = "http://lab.example:8800"
HEAD = "a" * 40
OTHER = "d" * 40
PARENT = "c" * 40
BASE = "b" * 40
ZERO = "0" * 40
ENVIRON = {post_receive.REPOSITORY: REPO, post_receive.ACTOR: ACTOR, post_receive.LAB_URL: LAB}


class Published:
    """A publisher that keeps every subject and envelope it is handed."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, Envelope]] = []

    def publish(self, subject: str, event: Envelope) -> bool:
        """Keep the subject and the envelope."""
        self.entries.append((subject, event))
        return True


def parent(sha: str) -> str:
    """Return a first parent that names its child."""
    return PARENT if sha == HEAD else sha[::-1]


def options(*values: str) -> dict[str, str]:
    """Return the environment git sets for the push options."""
    numbered = {f"{post_receive.OPTION}{index}": value for index, value in enumerate(values)}
    return {post_receive.OPTION_COUNT: str(len(values)), **numbered}


def receive(lines: list[str], environ: dict[str, str]) -> tuple[list[tuple[str, Envelope]], list[str]]:
    """Run the hook over the ref lines and return what it published and the lines it printed."""
    published, out = Published(), StringIO()
    assert post_receive.run(lines, environ, published, parent, out) == 0
    return published.entries, out.getvalue().splitlines()


def test_a_grade_ref_push_publishes_a_lab_head_with_its_ref_and_branch_and_prints_its_grade_url() -> None:
    entries, printed = receive([f"{ZERO} {HEAD} refs/grade/t1\n"], {**ENVIRON, **options("branch=engineer-1")})
    [(subject, event)] = entries
    assert subject == event.type == "ci.lab.head.notice"
    assert event.author == ACTOR
    assert event.key == f"{REPO}@{HEAD}"
    assert event.data == {
        "event": "notice",
        "repo": REPO,
        "head": HEAD,
        "base": PARENT,
        "actor": ACTOR,
        "action": "pushed",
        "head_branch": "engineer-1",
        "ref": "refs/grade/t1",
        "branch": "engineer-1",
    }
    assert printed == [f"lab grade: {LAB}/grades/{HEAD}"]


def test_the_base_push_option_wins_over_the_first_parent() -> None:
    entries, _ = receive([f"{OTHER} {HEAD} refs/grade/t1"], {**ENVIRON, **options("base=" + BASE, "branch=b")})
    assert entries[0][1].data["base"] == BASE


def test_a_push_without_options_grades_against_the_first_parent_with_no_branch() -> None:
    entries, _ = receive([f"{ZERO} {HEAD} refs/grade/t1"], ENVIRON)
    assert (entries[0][1].data["base"], entries[0][1].data["branch"], entries[0][1].data["head_branch"]) == (
        PARENT,
        "",
        "",
    )


@pytest.mark.parametrize("lab", [LAB, "http://lab.example/X"])
def test_the_grade_url_joins_a_lab_address_with_a_trailing_slash_once(lab: str) -> None:
    _, printed = receive([f"{ZERO} {HEAD} refs/grade/t1"], {**ENVIRON, post_receive.LAB_URL: f"{lab}/"})
    assert printed == [f"lab grade: {lab}/grades/{HEAD}"]


@pytest.mark.parametrize(
    "line",
    [f"{ZERO} {HEAD} refs/heads/dev", f"{ZERO} {HEAD} refs/tags/v1", f"{HEAD} {ZERO} refs/grade/t1"],
)
def test_another_ref_or_a_deleted_grade_ref_is_not_graded(line: str) -> None:
    entries, printed = receive([line], ENVIRON)
    ref = line.split()[2]
    assert entries == []
    assert printed == [f"lab remote: {ref} is not graded, only a pushed refs/grade/* head with a base is"]


def test_a_root_commit_pushed_without_a_base_option_is_not_graded() -> None:
    published, out = Published(), StringIO()
    post_receive.run([f"{ZERO} {HEAD} refs/grade/t1"], ENVIRON, published, lambda sha: "", out)
    assert published.entries == []
    assert out.getvalue() == "lab remote: refs/grade/t1 is not graded, only a pushed refs/grade/* head with a base is\n"


def test_the_same_head_on_another_branch_is_a_new_head_event_and_the_same_push_keeps_its_id() -> None:
    line = f"{ZERO} {HEAD} refs/grade/t1"
    first, _ = receive([line], {**ENVIRON, **options("branch=one")})
    again, _ = receive([line], {**ENVIRON, **options("branch=one")})
    other, _ = receive([line], {**ENVIRON, **options("branch=two")})
    moved, _ = receive([f"{ZERO} {HEAD} refs/grade/t2"], {**ENVIRON, **options("branch=one")})
    ids = [entries[0][1].id for entries in (first, again, other, moved)]
    assert ids[0] == ids[1]
    assert len({ids[0], ids[2], ids[3]}) == 3


def test_each_grade_ref_of_one_push_is_graded_and_a_blank_line_is_skipped() -> None:
    lines = [f"{ZERO} {HEAD} refs/grade/t1", "", f"{ZERO} {OTHER} refs/heads/x", f"{ZERO} {OTHER} refs/grade/t2"]
    entries, printed = receive(lines, ENVIRON)
    assert [(event.data["head"], event.data["ref"]) for _, event in entries] == [
        (HEAD, "refs/grade/t1"),
        (OTHER, "refs/grade/t2"),
    ]
    assert printed[0] == f"lab grade: {LAB}/grades/{HEAD}"
    assert printed[1].startswith("lab remote: refs/heads/x is not graded")
    assert printed[2] == f"lab grade: {LAB}/grades/{OTHER}"


def test_push_options_map_each_name_to_its_value_and_a_bare_name_to_empty_text() -> None:
    environ = options("branch=engineer-1", "base=a=b", "draft")
    assert post_receive.push_options(environ) == {"branch": "engineer-1", "base": "a=b", "draft": ""}
    assert post_receive.push_options({}) == {}


def test_the_scheduler_inbox_reads_a_graded_push_as_a_trusted_pull_request_head_on_its_branch_lane() -> None:
    bus = open_bus(BusConfig())
    consumer = bus.lab_heads.consumer(DURABLE, SUBJECT)
    environ = {**ENVIRON, **options("branch=engineer-1")}
    post_receive.run([f"{ZERO} {HEAD} refs/grade/t1"], environ, bus.lab_heads, parent, StringIO())
    [head] = BusInbox(cast(Consumer, consumer)).heads()
    assert (head.event.head, head.event.base, head.lane) == (HEAD, PARENT, "engineer-1")
    request = intake.admit(head.event, {ACTOR})
    assert request is not None
    assert (request.repo, request.tier) == (REPO, Priority.PULL_REQUEST)


def test_main_reads_the_ref_lines_from_stdin_and_prints_the_grade_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[BusConfig] = []

    def open_unprovisioned(config: BusConfig) -> Bus:
        opened.append(config)
        return cast(Bus, open_bus(config))

    monkeypatch.setattr(sys, "stdin", StringIO(f"{ZERO} {HEAD} refs/grade/t1\n"))
    monkeypatch.setattr(post_receive, "first_parent", parent)
    monkeypatch.setattr(post_receive, "open_bus", open_unprovisioned)
    assert post_receive.main(ENVIRON) == 0
    assert capsys.readouterr().out == f"lab grade: {LAB}/grades/{HEAD}\n"
    assert [config.provision for config in opened] == [False]


def _git(cwd: Path, *argv: str, extra: dict[str, str] | None = None) -> str:
    """Run git outside any outer repository and the global and system configuration; return its output and error text."""
    inherited = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environ = {**inherited, **(extra or {}), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run(["git", *argv], cwd=cwd, env=environ, capture_output=True, text=True, check=True)
    return done.stdout + done.stderr


def _commit(work: Path, name: str) -> str:
    """Commit one new file to the work repository and return the commit."""
    (work / name).write_text(name)
    _git(work, "add", name)
    _git(work, "-c", "user.name=lab", "-c", "user.email=lab@example.com", "commit", "--quiet", "-m", name)
    return _git(work, "rev-parse", "HEAD").strip()


def test_first_parent_names_a_commit_parent_and_empty_text_for_a_root_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(tmp_path, "init", "--quiet", str(tmp_path / "work"))
    root = _commit(tmp_path / "work", "first")
    child = _commit(tmp_path / "work", "second")
    monkeypatch.chdir(tmp_path / "work")
    assert (post_receive.first_parent(child), post_receive.first_parent(root)) == (root, "")


HOOK = """import json, os, sys
sys.path.insert(0, {root!r})
from scripts.ci_lab.remote import post_receive


class Recorder:
    def publish(self, subject, event):
        with open({record!r}, "a") as record:
            record.write(json.dumps(dict(subject=subject, data=event.data)) + "\\n")
        return True


sys.exit(post_receive.run(sys.stdin, os.environ, Recorder(), post_receive.first_parent, sys.stdout))
"""


def test_a_git_push_to_a_grade_ref_shows_the_grade_url_in_the_push_transcript(tmp_path: Path) -> None:
    lab, work, hooks, record = tmp_path / "lab.git", tmp_path / "work", tmp_path / "hooks", tmp_path / "record"
    hooks.mkdir()
    (hooks / "post_receive.py").write_text(HOOK.format(root=str(ROOT), record=str(record)))
    hook = hooks / "post-receive"
    hook.write_text(f"#!/usr/bin/env bash\nexec {shlex.quote(sys.executable)} {hooks / 'post_receive.py'}\n")
    hook.chmod(0o755)
    _git(tmp_path, "init", "--quiet", "--bare", str(lab))
    _git(lab, "config", "core.hooksPath", str(hooks))
    _git(lab, "config", "receive.advertisePushOptions", "true")
    _git(tmp_path, "init", "--quiet", str(work))
    base = _commit(work, "first")
    head = _commit(work, "second")
    transcript = _git(work, "push", "-o", "branch=engineer-1", str(lab), "HEAD:refs/grade/t1", extra=ENVIRON)
    assert f"remote: lab grade: {LAB}/grades/{head}" in transcript
    [published] = [json.loads(line) for line in record.read_text().splitlines()]
    assert published["subject"] == "ci.lab.head.notice"
    assert {name: published["data"][name] for name in ("head", "base", "ref", "branch")} == {
        "head": head,
        "base": base,
        "ref": "refs/grade/t1",
        "branch": "engineer-1",
    }
