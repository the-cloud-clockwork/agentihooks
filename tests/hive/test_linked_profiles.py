import json
import subprocess

import pytest

pytestmark = pytest.mark.unit


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def source(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "--initial-branch=dev")
    git(repo, "remote", "add", "origin", "git@github.com:example/capabilities.git")
    profile = repo / "profiles" / "memory"
    profile.mkdir(parents=True)
    (profile / "profile.yml").write_text("name: memory\n")
    git(repo, "add", "profiles/memory/profile.yml")
    git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture")
    home = tmp_path / "state"
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"linked_profiles": [{"name": "recall", "path": str(profile)}]}))
    return home, repo


def test_hive_setup_publishes_linked_profile_origins_and_relative_locations(tmp_path):
    from scripts.hive import setup

    home, _ = source(tmp_path)
    kept, skipped = setup.entries({}, home)
    assert skipped == {}
    assert json.loads(kept["HIVE_LINKED_PROFILES"]) == [
        {
            "name": "recall",
            "url": "https://github.com/example/capabilities.git",
            "ref": "dev",
            "path": "profiles/memory",
        }
    ]


def test_profile_install_clones_and_registers_the_alias_before_init(tmp_path, monkeypatch):
    from scripts.hive import linked_profiles

    _, repo = source(tmp_path)
    root = tmp_path / "hive"
    root.mkdir()
    spec = {"name": "recall", "url": str(repo), "ref": "dev", "path": "profiles/memory"}
    commands = []
    run = subprocess.run

    def execute(argv, **kwargs):
        commands.append(argv)
        if argv[0] == "agentihooks":
            return subprocess.CompletedProcess(argv, 0)
        return run(argv, **kwargs)

    monkeypatch.setattr(linked_profiles.subprocess, "run", execute)
    linked_profiles.install({"HIVE_REPO_ROOT": str(root), "HIVE_LINKED_PROFILES": json.dumps([spec])})
    profile = root / ".linked-profiles" / "recall" / "profiles" / "memory"
    assert (profile / "profile.yml").read_text() == "name: memory\n"
    assert commands[-1] == [
        "agentihooks",
        "link-profile",
        "link",
        str(profile),
        "--name",
        "recall",
        "--no-append",
        "--no-init",
    ]
    assert commands[0] == ["git", "clone", "--branch", "dev", "--", str(repo), str(root / ".linked-profiles/recall")]
    commands.clear()
    linked_profiles.install({"HIVE_REPO_ROOT": str(root), "HIVE_LINKED_PROFILES": json.dumps([spec])})
    assert len(commands) == 1


def test_no_linked_profiles_publish_an_empty_list_and_need_no_repository_root(tmp_path):
    from scripts.hive import linked_profiles, setup

    kept, _ = setup.entries({}, tmp_path)
    assert kept["HIVE_LINKED_PROFILES"] == "[]"
    linked_profiles.install({})


@pytest.mark.parametrize(
    "name,path", [("../escape", "profiles/memory"), ("recall", "../escape"), ("recall", "/outside")]
)
def test_install_refuses_profile_locations_outside_its_checkout(tmp_path, name, path):
    from scripts.hive import linked_profiles

    _, repo = source(tmp_path)
    spec = {"name": name, "url": str(repo), "ref": "dev", "path": path}
    with pytest.raises(ValueError, match="profile"):
        linked_profiles.install({"HIVE_REPO_ROOT": str(tmp_path), "HIVE_LINKED_PROFILES": json.dumps([spec])})


def test_linked_profile_without_a_repository_is_refused_instead_of_silently_omitted(tmp_path):
    from scripts.hive import auth, setup

    home = tmp_path / "state"
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"linked_profiles": [{"name": "memory", "path": str(tmp_path)}]}))
    with pytest.raises(auth.HiveError, match="linked profile"):
        setup.entries({}, home)


@pytest.mark.parametrize("change", ["remote", "branch"])
def test_profile_sources_without_a_remote_or_branch_are_refused(tmp_path, change):
    from scripts.hive import auth, linked_profiles

    home, repo = source(tmp_path)
    if change == "remote":
        git(repo, "remote", "remove", "origin")
    else:
        git(repo, "switch", "--detach")
    with pytest.raises(auth.HiveError, match="origin and a branch"):
        linked_profiles.sources(home)


def test_a_cloned_profile_symlink_cannot_escape_its_repository(tmp_path):
    from scripts.hive import linked_profiles

    _, repo = source(tmp_path)
    git(repo, "rm", "profiles/memory/profile.yml")
    (repo / "profiles/memory").symlink_to(tmp_path, target_is_directory=True)
    git(repo, "add", "profiles/memory")
    git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "symlink")
    spec = {"name": "recall", "url": str(repo), "ref": "dev", "path": "profiles/memory"}
    with pytest.raises(ValueError, match="symlink"):
        linked_profiles.install({"HIVE_REPO_ROOT": str(tmp_path), "HIVE_LINKED_PROFILES": json.dumps([spec])})
