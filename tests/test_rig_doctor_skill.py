import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

SKILL = Path(__file__).parents[1] / "profiles/package/skills/rig-doctor"


@pytest.fixture
def runner(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("rig_doctor", SKILL / "scripts/rig_doctor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "HOME", tmp_path)
    monkeypatch.setattr(module, "linked_bundle", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: calls.append(args) or "")
    return module, calls


def test_named_ledger_starts_and_records_active_doctor(runner):
    module, calls = runner
    assert module.main(["work"]) == 0
    assert calls == [("agentihooks", "doctor", "work", "start"), ("agentihooks", "doctor", "work", "status")]
    assert json.loads((module.HOME / "rig-doctor.json").read_text())["active"] == "work"
    assert not (module.HOME / "doctor-demo-app").exists()


def test_stop_closes_saved_doctor_without_stopping_watched_swarm(runner):
    module, calls = runner
    module.main(["work"])
    calls.clear()
    assert module.main(["stop"]) == 0
    assert calls == [("agentihooks", "doctor", "work", "stop")]
    assert json.loads((module.HOME / "rig-doctor.json").read_text())["active"] == ""


def test_start_refuses_without_bundle_before_mutating(runner, monkeypatch, capsys):
    module, calls = runner
    monkeypatch.setattr(module, "linked_bundle", lambda: None)
    assert module.main([]) == 1
    assert "agentihooks init --bundle" in capsys.readouterr().err
    assert calls == []
    assert not (module.HOME / "rig-doctor.json").exists()


def test_skill_passes_the_skill_standard():
    _, front, body = (SKILL / "SKILL.md").read_text().split("---", 2)
    meta = yaml.safe_load(front)
    assert re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"])
    assert meta["name"] == SKILL.name
    assert 0 < len(meta["description"]) <= 1024
    assert not re.search(r"[<>]", meta["description"])
    assert len(body.splitlines()) < 500
    evals = json.loads((SKILL / "evals/evals.json").read_text())
    assert len(evals) >= 3
    assert all(e["query"] and e["expected_behavior"] for e in evals)


def test_demo_dispatch_initializes_workload_before_doctor(runner, monkeypatch):
    module, calls = runner
    monkeypatch.setattr(module, "demo", lambda: "demo-work")
    assert module.main([]) == 0
    assert calls[0] == ("agentihooks", "doctor", "demo-work", "start")


def test_demo_reset_preserves_previous_copy_and_seeds_dev(runner, monkeypatch):
    module, calls = runner
    repo = module.HOME / "doctor-demo-app"
    repo.mkdir()
    (repo / "old.py").write_text("old application")
    (repo / ".rig-doctor-demo").write_text("owned\n")
    monkeypatch.setattr(module, "ensure_remote", lambda: ("owner/demo", False))
    module.reset_demo()
    assert not (repo / "old.py").exists()
    archives = list((module.HOME / "doctor-demo-archives").iterdir())
    assert len(archives) == 1 and (archives[0] / "old.py").read_text() == "old application"
    assert (repo / "CLAUDE.md").is_file()
    assert ("git", "init", "--initial-branch", "dev") in calls
    assert ("git", "push", "--set-upstream", "origin", "dev") in calls


def test_existing_remote_uses_dev_history_without_force_push(runner, monkeypatch):
    module, calls = runner
    monkeypatch.setattr(module, "ensure_remote", lambda: ("owner/demo", True))
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: calls.append(args) or "existing dev")
    module.reset_demo()
    assert ("git", "fetch", "origin", "dev") in calls
    assert ("git", "switch", "--create", "dev", "origin/dev") in calls
    assert all("--force" not in call for call in calls)


def test_unowned_demo_copy_is_refused(runner, monkeypatch):
    module, calls = runner
    repo = module.HOME / "doctor-demo-app"
    repo.mkdir()
    (repo / "unrelated.txt").write_text("preserve me")
    monkeypatch.setattr(module, "ensure_remote", lambda: ("owner/demo", False))
    with pytest.raises(ValueError, match="owned"):
        module.reset_demo()
    assert (repo / "unrelated.txt").read_text() == "preserve me"
    assert not calls


def test_public_remote_is_refused(runner, monkeypatch):
    module, _ = runner
    monkeypatch.setenv("RIG_DOCTOR_DEMO_REPO", "owner/demo")
    monkeypatch.setattr(
        module, "run", lambda *args, **kwargs: '{"isPrivate": false, "description": "rig-doctor demo workload"}'
    )
    with pytest.raises(ValueError, match="private"):
        module.ensure_remote()


def test_init_swarm_queues_all_template_tasks_and_starts_last(runner):
    module, calls = runner
    repo = module.HOME / "doctor-demo-app"
    repo.mkdir()
    slug = module.init_swarm(repo)
    template = json.loads((SKILL / "demo-template.json").read_text())
    additions = [c for c in calls if "task" in c and "add" in c]
    assert len(additions) == len(template["tasks"])
    assert calls[-2] == ("agentihooks", "swarm", slug, "start")
    assert calls[-1] == ("agentihooks", "swarm", slug, "status")
    assert "Tinder" in (repo / "plan.md").read_text()
    assert "Next.js" in (repo / "plan.md").read_text()
    assert "FastAPI" in (repo / "plan.md").read_text()
    assert "localhost" in (repo / "plan.md").read_text()


def test_demo_reset_really_pushes_clean_seed_on_existing_dev(runner, monkeypatch, tmp_path):
    import subprocess

    module, _ = runner
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    monkeypatch.setattr(module, "ensure_remote", lambda: ("owner/demo", False))

    def local_run(*args, cwd=None):
        if args[:3] == ("git", "remote", "add"):
            args = (*args[:-1], str(remote))
        done = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True)
        return done.stdout.strip()

    monkeypatch.setattr(module, "run", local_run)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Doctor test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "doctor@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Doctor test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "doctor@example.invalid")
    repo = module.reset_demo()
    (repo / "old.py").write_text("old app")
    local_run("git", "add", "--", "old.py", cwd=repo)
    local_run("git", "commit", "-m", "Build old app", cwd=repo)
    local_run("git", "push", "origin", "dev", cwd=repo)
    old_head = local_run("git", "rev-parse", "HEAD", cwd=repo)
    monkeypatch.setattr(module, "ensure_remote", lambda: ("owner/demo", True))
    module.reset_demo()
    assert not (repo / "old.py").exists()
    assert local_run("git", "rev-parse", "HEAD^", cwd=repo) == old_head
    assert local_run("git", "status", "--porcelain", cwd=repo) == ""
    assert local_run("git", "--git-dir", str(remote), "rev-parse", "refs/heads/dev") == local_run(
        "git", "rev-parse", "HEAD", cwd=repo
    )


def test_existing_empty_private_remote_gets_dev_branch(runner, monkeypatch):
    module, calls = runner
    monkeypatch.setattr(module, "ensure_remote", lambda: ("owner/demo", True))
    module.reset_demo()
    assert ("git", "fetch", "origin", "dev") not in calls
    assert ("git", "commit", "-m", "Reset rig-doctor demo seed") in calls
