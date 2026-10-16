import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import publish, stack
from scripts.swarm.store import AgentRecord

pytestmark = pytest.mark.unit


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    roots = []
    for name in ("agentihooks", "agentihooks-bundle"):
        root = tmp_path / name
        root.mkdir()
        git(root, "init", "-q", "-b", "dev")
        git(root, "config", "user.name", "test")
        git(root, "config", "user.email", "test@example.com")
        (root / "chosen.txt").write_text(name)
        git(root, "add", "chosen.txt")
        git(root, "commit", "-qm", "base")
        remote = tmp_path / f"{name}.git"
        git(root, "init", "--bare", str(remote))
        git(root, "remote", "add", "origin", str(remote))
        git(root, "push", "-u", "origin", "dev")
        roots.append(root)
    script = tmp_path / "wt.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        '[[ "$#" == 3 && "$1" == new && "$2" == --repo && "$PWD" == "$3" ]]\n'
        'git -C "$3" fetch origin dev >&2\n'
        'dest="$3-worktree"\n'
        'git -C "$3" worktree add -b engineer-test "$dest" origin/dev >&2\n'
        'printf "%s\\n" "$dest"\n'
    )
    monkeypatch.setattr(stack, "WT_SCRIPT", script)
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed"}
    updates = []

    def update(slug, task_id, fields, by):
        assert (slug, task_id, by) == ("swarm", "t1", task["claimed_by"])
        updates.append(fields)
        task.update(fields)

    return SimpleNamespace(
        swarm=roots[0],
        bundle=roots[1],
        task=task,
        agent=AgentRecord("engineer@test", "eng", "t1"),
        ledger=SimpleNamespace(tasks=lambda slug: [task], update_task=update),
        updates=updates,
    )


def test_prepare_uses_the_explicit_bundle_repository(repositories):
    p = repositories
    p.task["repositories"] = [str(p.bundle)]
    fields = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    assert fields["worktree"] == f"{p.bundle}-worktree"
    assert fields["repository_source"] == "task"
    assert git(p.bundle, "rev-parse", "origin/dev") == git(fields["worktree"], "rev-parse", "HEAD")
    assert git(fields["worktree"], "show", "HEAD:chosen.txt") == "agentihooks-bundle"
    assert publish.prepare("swarm", p.agent, str(p.swarm), p.ledger) == fields
    assert len(git(p.swarm, "worktree", "list", "--porcelain").split("worktree ")) == 2


def test_prepare_keeps_one_worktree_per_repository(repositories):
    p = repositories
    p.task["repositories"] = [str(p.swarm), str(p.bundle)]
    fields = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    roots = [entry["worktree"] for entry in fields["worktrees"]]
    assert roots == [f"{p.swarm}-worktree", f"{p.bundle}-worktree"]
    assert [git(root, "show", "HEAD:chosen.txt") for root in roots] == ["agentihooks", "agentihooks-bundle"]
    assert publish.prepare("swarm", p.agent, str(p.swarm), p.ledger) == fields
    assert p.task["repository_branches"] == {str(p.swarm): "engineer-test", str(p.bundle): "engineer-test"}


def test_prepare_names_the_default_repository_without_inferring_prose(repositories):
    p = repositories
    p.task["description"] = f"Work in {p.bundle}"
    fields = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    assert fields["worktree"] == f"{p.swarm}-worktree"
    assert fields["repository_source"] == "swarm default"
    assert fields["repository"] == str(p.swarm)


def install_gate(root):
    package = root / "scripts" / "ci_prepush"
    package.mkdir(parents=True)
    (root / "scripts" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        "from pathlib import Path\nimport subprocess\n"
        "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True)\n"
        "directory = subprocess.check_output(['git', 'rev-parse', '--absolute-git-dir'], text=True).strip()\n"
        "Path(directory, 'ci_prepush').write_text(head)\n"
    )
    git(root, "add", "scripts")
    git(root, "commit", "-qm", "Install repository gate")


def test_publish_selects_the_current_repository_and_retains_both_branches(repositories, monkeypatch):
    p = repositories
    p.task["repositories"] = [str(p.swarm), str(p.bundle)]
    fields = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    roots = [entry["worktree"] for entry in fields["worktrees"]]
    for root in roots:
        from pathlib import Path

        install_gate(Path(root))
        Path(root, "chosen.txt").write_text("after")
    git(roots[1], "branch", "-m", "engineer-test-2")
    fields["worktrees"][1]["branch"] = "engineer-test-2"
    monkeypatch.chdir(roots[1])
    first = publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Change bundle", ["chosen.txt"])
    assert first["worktree"] == roots[1]
    assert first["gate"] == str(p.bundle) + "-worktree/scripts/ci_prepush"
    assert git(roots[0], "show", "HEAD:chosen.txt") == "agentihooks"
    assert git(p.swarm, "ls-remote", "origin", "refs/heads/engineer-test") == ""
    monkeypatch.chdir(roots[0])
    second = publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Change hooks", ["chosen.txt"])
    assert second["worktree"] == roots[0]
    assert p.task["repository_branches"] == {str(p.bundle): "engineer-test-2", str(p.swarm): "engineer-test"}
    for root, repository, branch in zip(roots, (p.swarm, p.bundle), ("engineer-test", "engineer-test-2"), strict=True):
        assert git(repository, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == git(
            root, "rev-parse", "HEAD"
        )
    p.agent = AgentRecord("engineer@successor", "eng", "t1")
    p.task["claimed_by"] = p.agent.name
    assert publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktrees"] == fields["worktrees"]
    monkeypatch.chdir(roots[1])
    successor = publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Continue bundle", ["chosen.txt"])
    assert successor["branch"] == "engineer-test-2"
    p.task["repositories"] = [str(p.bundle)]
    monkeypatch.chdir(p.swarm)
    continued = publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Continue from primary", ["chosen.txt"])
    assert continued["worktree"] == roots[1]
    assert continued["branch"] == "engineer-test-2"


@pytest.mark.parametrize(
    "source, succeeds",
    [
        ("value = 1\n", True),
        ("value =\n", False),
        ("import os\n", False),
        ("value=1\n", False),
        ("def chosen():\n" + "    print(1)\n" * 300, False),
        ("def chosen():\n" + "    print(1)\n" * 299, True),
        ("print(1)\n" * 3000, True),
        ("print(1)\n" * 3001, False),
        ("def chosen(a, b, c, d, e, f, g, h):\n    return a, b, c, d, e, f, g, h\n", False),
        ("def chosen(a, b, c, d, e, f, g):\n    return a, b, c, d, e, f, g\n", True),
        (
            "def chosen(value):\n    result = 0\n"
            + "".join(f"    if value == {n}:\n        result += 1\n" for n in range(11))
            + "    return result\n",
            True,
        ),
    ],
    ids=[
        "valid",
        "syntax",
        "lint",
        "format",
        "function_length",
        "function_limit",
        "file_limit",
        "file_length",
        "arguments",
        "argument_limit",
        "complexity_limit",
    ],
)
def test_bundle_without_a_gate_uses_the_named_touched_file_fallback(
    repositories, monkeypatch, capsys, source, succeeds
):
    from pathlib import Path

    from scripts.swarm.store import SwarmError

    p = repositories
    (p.bundle / "untouched.py").write_text("broken =\n")
    git(p.bundle, "add", "untouched.py")
    git(p.bundle, "commit", "-qm", "Existing unrelated source")
    git(p.bundle, "push", "origin", "dev")
    p.task["repositories"] = [str(p.bundle)]
    root = Path(publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"])
    (root / "chosen.py").write_text(source)
    (root / "chosen.txt").write_text("not Python source\n")
    git(root, "add", "chosen.py", "chosen.txt")
    git(root, "commit", "-qm", "Commit source for grading")
    monkeypatch.chdir(root)
    from scripts import ci_prepush
    from scripts.swarm import publish_gate

    assert publish_gate.main() == (0 if succeeds else 1)
    output = capsys.readouterr().out
    assert output.startswith(f"AgentiHooks touched Python fallback: {root}\n")
    if succeeds:
        assert output.endswith("Graded 1 changed Python files\n")
    assert ci_prepush.passed(root) is succeeds
    ci_prepush.stamp_path(root).unlink(missing_ok=True)
    monkeypatch.chdir(p.swarm)
    if succeeds:
        result = publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Change bundle", ["chosen.py"])
        assert result["gate"] == "AgentiHooks touched Python fallback"
        assert result["worktree"] == str(root)
        assert git(p.bundle, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == result["head"]
    else:
        with pytest.raises(SwarmError) as error:
            publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Bad bundle", ["chosen.py"])
        assert str(error.value).startswith(f"AgentiHooks touched Python fallback for {root}: ")
        assert git(p.bundle, "ls-remote", "origin", "refs/heads/engineer-test") == ""


@pytest.mark.parametrize("shape", ["script", "package"])
def test_native_gate_without_a_scripts_package_runs_its_own_entry_point(repositories, monkeypatch, shape):
    from pathlib import Path

    p = repositories
    p.task["repositories"] = [str(p.bundle)]
    root = Path(publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"])
    install_gate(root)
    (root / "scripts" / "__init__.py").unlink()
    if shape == "script":
        source = root / "scripts" / "ci_prepush"
        source.with_suffix(".py").write_text((source / "__main__.py").read_text())
        (source / "__main__.py").unlink()
        (source / "__init__.py").unlink()
        source.rmdir()
    git(root, "add", "scripts")
    git(root, "commit", "-qm", "Use namespace scripts directory")
    installed = p.bundle.parent / "installed"
    (installed / "scripts").mkdir(parents=True)
    (installed / "scripts" / "__init__.py").write_text("")
    (installed / "scripts" / "ci_prepush.py").write_text("raise SystemExit('foreign installed gate selected')\n")
    monkeypatch.setenv("PYTHONPATH", str(installed))
    (root / "chosen.txt").write_text("after")
    monkeypatch.chdir(root)
    result = publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Use native gate", ["chosen.txt"])
    suffix = "scripts/ci_prepush.py" if shape == "script" else "scripts/ci_prepush"
    assert result["gate"] == str(root / suffix)
    assert git(p.bundle, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == result["head"]


def test_publish_from_primary_requires_a_repository_choice(repositories, monkeypatch):
    from scripts.swarm.store import SwarmError

    p = repositories
    p.task["repositories"] = [str(p.swarm), str(p.bundle)]
    publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    monkeypatch.chdir(p.swarm)
    with pytest.raises(SwarmError) as error:
        publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Choose repository", ["chosen.txt"])
    assert str(error.value) == "publish from the task worktree to select one of its repositories"


def test_publish_refuses_a_current_worktree_outside_the_task_repositories(repositories, monkeypatch):
    from scripts.swarm.store import SwarmError

    p = repositories
    root = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"]
    p.task["repositories"] = [str(p.bundle)]
    monkeypatch.chdir(root)
    with pytest.raises(SwarmError) as error:
        publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Wrong repository", ["chosen.txt"])
    assert str(error.value) == f"current worktree repository {p.swarm} is not in the task repositories"


def test_publish_refuses_an_unowned_current_worktree(repositories, monkeypatch):
    from scripts.swarm.store import SwarmError

    p = repositories
    root = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"]
    git(root, "branch", "-m", "unrelated-task")
    monkeypatch.chdir(root)
    with pytest.raises(SwarmError) as error:
        publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Wrong task", ["chosen.txt"])
    assert str(error.value) == f"current worktree {root} does not belong to this task"


def test_prepare_scopes_legacy_branch_records_to_their_repository(repositories):
    p = repositories
    p.task.update(
        repositories=[str(p.bundle)], branch="foreign-task", branch_repo=git(p.swarm, "remote", "get-url", "origin")
    )
    result = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    assert result["worktree"] == f"{p.bundle}-worktree"
    assert git(result["worktree"], "rev-parse", "HEAD") == git(p.bundle, "rev-parse", "origin/dev")


def test_prepare_ignores_an_unscoped_branch_for_multiple_repositories(repositories):
    p = repositories
    p.task.update(repositories=[str(p.swarm), str(p.bundle)], branch="ambiguous-task")
    result = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    assert [entry["worktree"] for entry in result["worktrees"]] == [f"{p.swarm}-worktree", f"{p.bundle}-worktree"]


@pytest.mark.parametrize("record_remote", [False, True])
def test_prepare_reuses_a_legacy_branch_with_one_explicit_repository(repositories, monkeypatch, record_remote):
    p = repositories
    p.task["repositories"] = [str(p.bundle)]
    root = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"]
    git(root, "branch", "-m", "prior-task-branch")
    p.task["branch"] = "prior-task-branch"
    p.task.pop("repository_branches", None)
    p.task.pop("branch_repo", None)
    if record_remote:
        p.task["branch_repo"] = git(p.bundle, "remote", "get-url", "origin")
    monkeypatch.chdir(p.swarm)
    result = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    assert result["worktree"] == root
    assert result["branch"] == "prior-task-branch"


def test_fallback_grades_committed_python_files_and_ignores_deleted_untracked_source(repositories, monkeypatch, capsys):
    from pathlib import Path

    from scripts.swarm import publish_gate

    p = repositories
    (p.bundle / "deleted.py").write_text("value = 1\n")
    git(p.bundle, "add", "deleted.py")
    git(p.bundle, "commit", "-qm", "Base source")
    git(p.bundle, "push", "origin", "dev")
    p.task["repositories"] = [str(p.bundle)]
    root = Path(publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"])
    git(root, "rm", "deleted.py")
    (root / "chosen.py").write_text("value = 2\n")
    git(root, "add", "chosen.py")
    git(root, "commit", "-qm", "Replace source")
    (root / "deleted.py").write_text("invalid =\n")
    monkeypatch.chdir(root)
    assert publish_gate.main() == 0
    assert capsys.readouterr().out == f"AgentiHooks touched Python fallback: {root}\nGraded 1 changed Python files\n"


def test_fallback_reports_each_size_violation(repositories, monkeypatch, capsys):
    from pathlib import Path

    from scripts.swarm import publish_gate

    p = repositories
    p.task["repositories"] = [str(p.bundle)]
    root = Path(publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"])
    (root / "chosen.py").write_text("def chosen():\n" + "    print(1)\n" * 3001)
    git(root, "add", "chosen.py")
    git(root, "commit", "-qm", "Oversized source")
    monkeypatch.chdir(root)
    assert publish_gate.main() == 1
    assert capsys.readouterr().out == (
        f"AgentiHooks touched Python fallback: {root}\n"
        "chosen.py: file exceeds 3000 lines\nchosen.py: chosen exceeds 300 lines\n"
    )


def test_fallback_selects_the_committed_diff_when_working_source_is_reverted(repositories, monkeypatch, capsys):
    from pathlib import Path

    from scripts.swarm import publish_gate

    p = repositories
    (p.bundle / "chosen.py").write_text("value = 1\n")
    git(p.bundle, "add", "chosen.py")
    git(p.bundle, "commit", "-qm", "Base source")
    git(p.bundle, "push", "origin", "dev")
    p.task["repositories"] = [str(p.bundle)]
    root = Path(publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"])
    (root / "chosen.py").write_text("value = 2\n")
    git(root, "add", "chosen.py")
    git(root, "commit", "-qm", "Changed source")
    (root / "chosen.py").write_text("value = 1\n")
    monkeypatch.chdir(root)
    assert publish_gate.main() == 0
    assert capsys.readouterr().out == f"AgentiHooks touched Python fallback: {root}\nGraded 1 changed Python files\n"


def test_fallback_does_not_start_an_interactive_python(repositories, monkeypatch):
    import sys
    from pathlib import Path

    p = repositories
    p.task["repositories"] = [str(p.bundle)]
    root = Path(publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"])
    (root / "chosen.py").write_text("value = 1\n")
    wrapper = p.bundle.parent / "noninteractive-python"
    wrapper.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'for arg in "$@"; do [[ "$arg" != -i ]]; done\n'
        'exec "$PUBLISH_REAL_PYTHON" "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PUBLISH_REAL_PYTHON", sys.executable)
    monkeypatch.setattr(publish, "gate_python", lambda repository: str(wrapper))
    monkeypatch.chdir(p.swarm)
    result = publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Noninteractive gate", ["chosen.py"])
    assert git(p.bundle, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == result["head"]


def test_fallback_applies_size_limits_even_when_repository_config_ignores_them(repositories, monkeypatch):
    from pathlib import Path

    from scripts.swarm import publish_gate

    p = repositories
    (p.bundle / ".ruff.toml").write_text('[lint.per-file-ignores]\n"chosen.py" = ["PLR0913"]\n')
    git(p.bundle, "add", ".ruff.toml")
    git(p.bundle, "commit", "-qm", "Existing lint configuration")
    git(p.bundle, "push", "origin", "dev")
    p.task["repositories"] = [str(p.bundle)]
    root = Path(publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"])
    (root / "chosen.py").write_text("def chosen(a, b, c, d, e, f, g, h):\n    return a, b, c, d, e, f, g, h\n")
    git(root, "add", "chosen.py")
    git(root, "commit", "-qm", "Excess arguments")
    monkeypatch.chdir(root)
    assert publish_gate.main() == 1


def test_fallback_grades_a_literal_leading_dash_filename(repositories, monkeypatch):
    from pathlib import Path

    from scripts.swarm import publish_gate

    p = repositories
    p.task["repositories"] = [str(p.bundle)]
    root = Path(publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"])
    (root / "-chosen.py").write_text("value = 1\n")
    git(root, "add", "--", "-chosen.py")
    git(root, "commit", "-qm", "Literal source filename")
    monkeypatch.chdir(root)
    assert publish_gate.main() == 0
