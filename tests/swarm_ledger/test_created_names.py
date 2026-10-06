import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import gc_cli
from scripts.swarm import naming
from scripts.swarm.runtime import herdr_target
from scripts.swarm_ledger import new_ledger
from tests.lifecycle.test_run_cli import snap

AGENT = {"AGENTIHOOKS_AGENT_NAME": "engineer@a1b2c3-0002", "AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_SWARM_TASK": "dn1"}
SESSION = {"CLAUDE_CODE_SESSION_ID": "d540c686-0d5b-4ab1-ac24-fce8f92e2131"}


def matches(kind, name):
    return naming.PATTERNS[kind].fullmatch(name) is not None


@pytest.fixture
def repo(tmp_path):
    primary = tmp_path / "agentihooks"
    subprocess.run(["git", "init", "-q", str(primary)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(primary),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "i",
        ],
        check=True,
    )
    linked = tmp_path / "wt" / "engineer-a1b2c3-0002"
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "x", str(linked)], check=True)
    return primary, linked


@pytest.fixture
def new_cli(monkeypatch, tmp_path):
    content = tmp_path / "content.json"
    content.write_text(json.dumps({"title": "Proof", "overview": "A proof.", "phases": [{"title": "One"}]}))

    def run(*argv, environ=None):
        for key in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(key, raising=False)
        for key, value in (environ or {}).items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(sys, "argv", ["new_ledger", "--content", str(content), *argv])
        new_ledger.main()

    return run


def created_slug(capsys):
    return json.loads(capsys.readouterr().out.splitlines()[0])["slug"]


def test_the_repo_name_comes_from_git_not_from_the_folder_a_command_runs_in(repo, tmp_path):
    primary, linked = repo
    assert naming.repo_name(primary) == "agentihooks"
    assert naming.repo_name(linked) == "agentihooks"
    assert naming.repo_name(tmp_path / "Not A Repo") == "not-a-repo"


def test_the_session_base_is_the_plain_agent_name_or_the_session_id():
    assert naming.session_base(AGENT) == "engineer-a1b2c3-0002"
    assert naming.session_base(SESSION) == "session-d540c686"
    assert naming.session_base({**SESSION, "AGENTIHOOKS_AGENT_NAME": "my-own-name"}) == "session-d540c686"
    with pytest.raises(naming.NamingError):
        naming.session_base({})


def test_worktree_names_are_the_base_then_a_counter():
    assert naming.worktree(AGENT) == "engineer-a1b2c3-0002"
    assert naming.worktree(AGENT, taken={"engineer-a1b2c3-0002"}) == "engineer-a1b2c3-0002-2"
    assert naming.tmp_worktree(AGENT, taken={"engineer-a1b2c3-0002-tmp-1"}) == "engineer-a1b2c3-0002-tmp-2"
    assert naming.is_worktree("engineer-a1b2c3-0002-3", AGENT)
    assert not naming.is_worktree("fix-the-thing", AGENT)


def test_name_worktree_prints_the_next_free_name_and_checks_a_given_one(tmp_path, monkeypatch, capsys, repo):
    primary, _ = repo
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path / "trees"))
    for key, value in AGENT.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "trees" / "agentihooks" / "engineer-a1b2c3-0002").mkdir(parents=True)
    assert gc_cli.main(["name", "worktree", "--repo", str(primary)]) == 0
    assert capsys.readouterr().out.strip() == "engineer-a1b2c3-0002-2"
    assert gc_cli.main(["name", "tmp", "--repo", str(primary)]) == 0
    assert capsys.readouterr().out.strip() == "engineer-a1b2c3-0002-tmp-1"
    assert gc_cli.main(["name", "worktree", "--repo", str(primary), "--check", "engineer-a1b2c3-0002"]) == 0
    assert gc_cli.main(["name", "worktree", "--repo", str(primary), "--check", "my-branch"]) == 1
    assert "engineer-a1b2c3-0002-2" in capsys.readouterr().err


def test_scratch_new_builds_the_task_folder_and_refuses_another_name(monkeypatch, tmp_path, capsys, repo):
    _, linked = repo
    monkeypatch.chdir(linked)
    monkeypatch.setattr(gc_cli, "take_snapshot", lambda: snap())
    monkeypatch.setattr(gc_cli, "owner_holder", lambda _snap: None)
    for key, value in AGENT.items():
        monkeypatch.setenv(key, value)
    assert gc_cli.main(["scratch", "new"]) == 0
    created = Path(capsys.readouterr().out.strip())
    assert created.parts[-2:] == ("agentihooks", "rig-dn1") and created.is_dir()
    assert matches("scratch", "/".join(created.parts[-2:]))
    assert gc_cli.main(["scratch", "new", "agentihooks/rig-dn1"]) == 0
    capsys.readouterr()
    assert gc_cli.main(["scratch", "new", "agentihooks/my-notes"]) == 1
    assert "agentihooks/rig-dn1" in capsys.readouterr().err


def test_ledger_new_refuses_a_free_text_slug(new_cli, capsys):
    with pytest.raises(SystemExit) as refused:
        new_cli("--slug", "br2-brain-proof", "--size", "swarm", environ=AGENT)
    assert "names come from code" in str(refused.value)


def test_a_proof_ledger_is_named_from_the_swarm_code_and_task_with_a_counter(new_cli, capsys):
    new_cli("--proof", "--size", "swarm", environ=AGENT)
    first = created_slug(capsys)
    new_cli("--proof", "--size", "swarm", environ=AGENT)
    assert (first, created_slug(capsys)) == ("proof-a1b2c3-dn1-1", "proof-a1b2c3-dn1-2")
    assert matches("proof_slug", first)
    with pytest.raises(SystemExit):
        new_cli("--proof", "--size", "swarm", environ=SESSION)


def test_a_small_ledger_is_named_from_the_session(new_cli, capsys):
    new_cli("--size", "small", "--as", "engineer@a1b2c3-0002", environ=SESSION)
    slug = created_slug(capsys)
    assert slug == "small-session-d540c686" and matches("small_slug", slug)


def test_a_plan_ledger_is_named_from_the_plan_file_and_date(new_cli, capsys):
    new_cli("--plan", "/plans/Mossy Rabin.md", "--date", "2026-10-06", "--size", "swarm", environ=SESSION)
    slug = created_slug(capsys)
    assert slug == "mossy-rabin-2026-10-06" and matches("plan_slug", slug)


def test_a_built_slug_is_still_accepted_by_name(new_cli, capsys):
    new_cli("--slug", "doctor-demo-20261006093000123456", "--size", "swarm", environ=SESSION)
    assert created_slug(capsys) == "doctor-demo-20261006093000123456"


def test_herdr_workspace_label_and_pane_name_are_built(repo):
    _, linked = repo
    assert naming.space(linked, "a1b2c3", "rig-grade-swarm") == "agentihooks-a1b2c3"
    assert naming.space(linked, "f2c2d8", "proof-a1b2c3-dn1-1") == "proof-a1b2c3-dn1-1"
    assert matches("space", naming.space(linked, "a1b2c3", "rig-grade-swarm"))
    assert matches("pane", herdr_target("engineer@a1b2c3-0002"))
    assert matches("agent", "engineer@a1b2c3-0002")


def test_every_created_name_kind_has_a_pattern():
    assert set(naming.PATTERNS) == {
        "agent",
        "pane",
        "space",
        "worktree",
        "tmp",
        "scratch",
        "plan_slug",
        "small_slug",
        "proof_slug",
        "demo_slug",
    }
    assert matches("worktree", naming.worktree(SESSION))
    assert matches("tmp", naming.tmp_worktree(SESSION))
    assert matches("demo_slug", "doctor-demo-20261006093000123456")
