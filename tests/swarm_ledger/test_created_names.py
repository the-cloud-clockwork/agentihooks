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


def test_counters_take_the_first_free_number_however_many_are_taken():
    taken = {"engineer-a1b2c3-0002", "engineer-a1b2c3-0002-2"}
    assert naming.worktree(AGENT, taken=taken) == "engineer-a1b2c3-0002-3"
    tmp = {"engineer-a1b2c3-0002-tmp-1", "engineer-a1b2c3-0002-tmp-2"}
    assert naming.tmp_worktree(AGENT, taken=tmp) == "engineer-a1b2c3-0002-tmp-3"
    assert naming.proof_slug(AGENT, taken={"proof-a1b2c3-dn1-1", "proof-a1b2c3-dn1-2"}) == "proof-a1b2c3-dn1-3"


def test_the_repo_name_from_a_subfolder_and_a_folder_with_no_usable_name(repo, tmp_path):
    primary, _ = repo
    (primary / "docs").mkdir()
    assert naming.repo_name(primary / "docs") == "agentihooks"
    assert naming.repo_name(tmp_path / "-.My Repo.-") == "my-repo"
    assert naming.repo_name(tmp_path / "---") == "repo"


def test_the_session_id_is_lowered_and_stripped_to_eight_hex_characters():
    assert naming.session_base({"CLAUDE_CODE_SESSION_ID": "D540C6-86-0D5B"}) == "session-d540c686"
    assert naming.session_base({"CLAUDE_CODE_SESSION_ID": "ABCDEF12"}) == "session-abcdef12"
    with pytest.raises(naming.NamingError, match="no swarm agent name and no session id"):
        naming.session_base({"CLAUDE_CODE_SESSION_ID": "abcdef1"})


def test_a_scratch_folder_needs_both_swarm_and_task_else_it_uses_the_session(repo):
    _, linked = repo
    assert naming.scratch({**SESSION, "AGENTIHOOKS_SWARM": "rig"}, linked) == "agentihooks/session-d540c686"
    assert naming.scratch({**SESSION, "AGENTIHOOKS_SWARM_TASK": "dn1"}, linked) == "agentihooks/session-d540c686"
    assert naming.scratch({**AGENT, "AGENTIHOOKS_SWARM_TASK": "DN 1"}, linked) == "agentihooks/rig-dn-1"


def test_plan_slugs_trim_separators_and_fall_back_to_plan():
    assert naming.plan_slug("/plans/--A b--.md", "2026-10-06") == "a-b-2026-10-06"
    assert naming.plan_slug("/plans/!!!.md", "2026-10-06") == "plan-2026-10-06"


def test_a_proof_slug_needs_the_agent_and_the_task():
    assert naming.proof_slug({**AGENT, "AGENTIHOOKS_SWARM_TASK": "DN-1"}) == "proof-a1b2c3-dn1-1"
    message = "a proof swarm is named from its swarm agent and task: create it from a swarm task session"
    for environ in ({"AGENTIHOOKS_AGENT_NAME": AGENT["AGENTIHOOKS_AGENT_NAME"]}, {"AGENTIHOOKS_SWARM_TASK": "dn1"}):
        with pytest.raises(naming.NamingError) as refused:
            naming.proof_slug(environ)
        assert str(refused.value) == message


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


def test_name_reads_the_default_worktree_root_and_the_current_repo(tmp_path, monkeypatch, capsys, repo):
    _, linked = repo
    monkeypatch.delenv("WORKTREE_ROOT", raising=False)
    monkeypatch.chdir(linked)
    for key, value in AGENT.items():
        monkeypatch.setenv(key, value)
    trees = Path.home() / "dev" / "worktrees" / "agentihooks"
    (trees / "engineer-a1b2c3-0002").mkdir(parents=True)
    (trees / "_tmp" / "engineer-a1b2c3-0002-tmp-1").mkdir(parents=True)
    assert gc_cli.main(["name", "worktree"]) == 0
    assert capsys.readouterr().out == "engineer-a1b2c3-0002-2\n"
    assert gc_cli.main(["name", "tmp"]) == 0
    assert capsys.readouterr().out == "engineer-a1b2c3-0002-tmp-2\n"


def test_name_checks_a_tmp_name_against_the_tmp_form(monkeypatch, capsys):
    for key, value in AGENT.items():
        monkeypatch.setenv(key, value)
    assert gc_cli.main(["name", "tmp", "--check", "engineer-a1b2c3-0002-tmp-1"]) == 0
    assert gc_cli.main(["name", "tmp", "--check", "engineer-a1b2c3-0002"]) == 1
    assert gc_cli.main(["name", "worktree", "--check", "engineer-a1b2c3-0002-tmp-1"]) == 1
    with pytest.raises(SystemExit):
        gc_cli.main(["name", "branch"])


def test_name_without_a_session_exits_three_on_stderr(monkeypatch, capsys):
    for key in ("AGENTIHOOKS_AGENT_NAME", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    assert gc_cli.main(["name", "worktree"]) == 3
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("name: no swarm agent name")


@pytest.mark.parametrize("command", ["gc", "lease", "scratch", "name"])
def test_the_agentihooks_cli_hands_each_workspace_command_to_gc_cli(command, monkeypatch):
    from scripts import install

    seen = []
    monkeypatch.setattr(gc_cli, "main", lambda argv: seen.append(argv) or 7)
    monkeypatch.setattr(sys, "argv", ["agentihooks", command, "x"])
    with pytest.raises(SystemExit) as done:
        install.main()
    assert (done.value.code, seen) == (7, [[command, "x"]])


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
    assert str(refused.value) == (
        "ledger names come from code: give --plan <file>, --proof from a swarm task session, or no name for a "
        "small ledger"
    )


def test_a_refused_proof_name_says_why(new_cli):
    with pytest.raises(SystemExit) as refused:
        new_cli("--proof", "--size", "swarm", environ=SESSION)
    assert str(refused.value).startswith("a proof swarm is named from its swarm agent and task")


def test_a_built_slug_for_a_small_ledger_is_kept(new_cli, capsys):
    new_cli("--slug", "doctor-demo-20261006093000999999", "--size", "small", "--as", "w", environ=SESSION)
    assert created_slug(capsys) == "doctor-demo-20261006093000999999"


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
    assert naming.space(linked, "a1b2c3") == "agentihooks-a1b2c3"
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
