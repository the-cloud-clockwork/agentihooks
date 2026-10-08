import json
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from hooks.context import project_identity


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def checkout(path, remote):
    path.mkdir(parents=True)
    git(path, "init")
    git(path, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "--allow-empty", "-m", "fixture")
    git(path, "remote", "add", "origin", remote)
    return path


@pytest.fixture
def repositories(tmp_path):
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/swarm_v2/project-identity.json").read_text())
    first, second, clone = [checkout(tmp_path / row["folder"], row["remote"]) for row in fixture["repositories"]]
    nested = first / "nested"
    nested.mkdir()
    linked = tmp_path / "linked"
    git(first, "worktree", "add", "-b", "fixture", str(linked))
    return first, second, clone, nested, linked


@pytest.mark.parametrize("repeat", range(2))
def test_canonical_checkout_worktree_nested_and_owner_isolation(repositories, repeat):
    first, second, clone, nested, linked = repositories
    for folder in (first, clone, nested, linked):
        identity = project_identity.resolve_project(str(folder), {})
        assert identity.project_id == "github.com/first/common"
        assert identity.attributes()["project_id"] == identity.project_id
        assert identity.project_identity_ambiguities_total == 0
    assert project_identity.resolve_project(str(second), {}).project_id == "github.com/second/common"
    assert project_identity.resolve_project(str(first), {}).project == "common"
    assert project_identity.resolve_project(str(linked), {}).worktree == "linked"


def test_swarm_does_not_confuse_equal_repository_basenames(monkeypatch, tmp_path, repositories):
    first, second, _, _, linked = repositories
    config = tmp_path / "state" / "swarm" / "fixture" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"repo": str(first)}))
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / "state")
    env = {"AGENTIHOOKS_SWARM": "fixture"}
    assert project_identity.resolve_project(str(linked), env).worktree == "linked"
    nested_identity = project_identity.resolve_project(str(first / "nested"), env)
    assert nested_identity.worktree == ""
    assert nested_identity.cwd == str(first / "nested")
    identity = project_identity.resolve_project(str(second), env)
    assert identity.project_id == "github.com/first/common"
    assert identity.worktree == ""
    assert identity.cwd == str(second)
    assert project_identity.resolve_project("", env).cwd == str(first)


@pytest.mark.parametrize(
    "remote",
    [
        pytest.param("https://user:fixture-password@github.com/first/common.git", id="https-userinfo"),
        pytest.param("ssh://user:fixture-password@github.com/first/common.git", id="ssh-userinfo"),
        "https://github.com/first/common.git?credential=fixture-value",
        "https://github.com/first/common#fragment",
        "file:///first/common",
        "../first/common",
        "github.com/first/common",
        "git@github.com/first/common",
        "https://github.com/group/subgroup/common",
        "https://github.com/../common",
        "https://github.com/first/.git",
        "https://github.com/./common",
        "https://github.com/first/.",
        "https://github.com/first/..",
    ],
)
def test_unsafe_or_ambiguous_remotes_remain_unknown_and_unstored(tmp_path, remote, request):
    repo = checkout(tmp_path / "common", remote)
    config = (repo / ".git" / "config").read_bytes()
    identity = project_identity.resolve_project(str(repo), {})
    assert identity.project_id == "unknown"
    assert identity.remote == ""
    assert identity.project_identity_ambiguities_total == 1
    request.node.user_properties.append(
        ("project_identity_ambiguities_total", identity.project_identity_ambiguities_total)
    )
    assert remote not in json.dumps(identity.attributes())
    assert (repo / ".git" / "config").read_bytes() == config
    git(repo, "remote", "set-url", "origin", "https://github.com/first/common.git")
    assert project_identity.resolve_project(str(repo), {}).project_id == "github.com/first/common"


@pytest.mark.parametrize("folder,project", [("plain", "plain"), ("scratchpad/common/task", "common")])
def test_explicit_non_git_registration_is_stable(monkeypatch, tmp_path, folder, project):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / folder
    path.mkdir(parents=True)
    env = {"AGENTIHOOKS_PROJECT_ID": "local:registered-project"}
    identity = project_identity.resolve_project(str(path), env)
    assert identity.project_id == "local:registered-project"
    assert identity.cwd == str(path)
    assert identity.project == project
    assert identity.repo == project
    assert identity.project_identity_ambiguities_total == 0
    unregistered = project_identity.resolve_project(str(path), {})
    assert unregistered is None or unregistered.project_id == "unknown"


@pytest.mark.parametrize("project_id", ["common", "local:", "local:../common", "github.com/first/common"])
def test_invalid_non_git_registration_is_refused(tmp_path, project_id):
    with pytest.raises(ValueError, match="^Invalid registered project ID$"):
        project_identity.resolve_project(str(tmp_path), {"AGENTIHOOKS_PROJECT_ID": project_id})


def test_git_remote_cannot_be_overridden_by_local_registration(repositories):
    first = repositories[0]
    identity = project_identity.resolve_project(str(first), {"AGENTIHOOKS_PROJECT_ID": "local:registered-project"})
    assert identity.project_id == "github.com/first/common"


@pytest.mark.parametrize("repeat", range(2))
def test_alias_rename_recovery_and_rollback(repositories, tmp_path, repeat):
    first, second, _, nested, linked = repositories
    historical = project_identity.resolve_project(str(first), {}).attributes()
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/swarm_v2/project-identity.json").read_text())
    aliases = fixture["alias_map"]
    alias_file = tmp_path / "aliases.json"
    alias_file.write_text(json.dumps(aliases))
    git(first, "remote", "set-url", "origin", fixture["renamed_remote"])
    config = (first / ".git" / "config").read_bytes()
    for folder in (first, nested, linked):
        recovered = project_identity.resolve_project(str(folder), {}, aliases=json.loads(alias_file.read_text()))
        assert recovered.project_id == historical["project_id"]
        assert recovered.remote == "first/renamed"
        assert recovered.project_identity_ambiguities_total == 0
    assert project_identity.resolve_project(str(second), {}, aliases=aliases).project_id == "github.com/second/common"
    assert (first / ".git" / "config").read_bytes() == config
    assert json.loads(alias_file.read_text()) == aliases
    legacy = {key: value for key, value in recovered.attributes().items() if key != "project_id"}
    assert set(legacy) == {"project", "repo", "cwd", "remote", "worktree"}
    assert legacy["project"] == historical["project"]
    assert historical["project_id"] == "github.com/first/common"
    assert project_identity.resolve_project(str(first), {}).project_id == "github.com/first/renamed"


@pytest.mark.parametrize(
    "aliases",
    [
        {"schema_version": "1.0", "aliases": {}},
        {"schema_version": "2.0", "aliases": []},
        {"schema_version": "2.0", "aliases": {"unknown": "github.com/first/common"}},
        {"schema_version": "2.0", "aliases": {"github.com/first/common": "unknown"}},
        {"schema_version": "2.0", "aliases": {"github.com/other/common": "github.com/other/common"}},
        {"schema_version": "2.0", "aliases": {"common": "github.com/first/common"}},
        {"schema_version": "2.0", "aliases": {"github.com/first/common": "local:other"}},
        {"schema_version": "2.0", "aliases": {"github.com/first/common": "github.com/first/common"}},
        {
            "schema_version": "2.0",
            "aliases": {
                "github.com/first/common": "github.com/first/new",
                "github.com/first/new": "github.com/first/common",
            },
        },
    ],
)
def test_invalid_alias_maps_are_refused_without_mutation(repositories, aliases):
    first = repositories[0]
    config = (first / ".git" / "config").read_bytes()
    original = json.dumps(aliases)
    with pytest.raises(ValueError, match="^Invalid project alias map(: cycle)?$"):
        project_identity.resolve_project(str(first), {}, aliases=aliases)
    assert json.dumps(aliases) == original
    assert (first / ".git" / "config").read_bytes() == config


def test_alias_chain_is_replayed_without_changing_its_map(repositories):
    first = repositories[0]
    aliases = {
        "schema_version": "2.0",
        "aliases": {
            "github.com/first/common": "github.com/first/intermediate",
            "github.com/first/intermediate": "github.com/first/current",
        },
    }
    for _ in range(2):
        assert (
            project_identity.resolve_project(str(first), {}, aliases=aliases).project_id == "github.com/first/current"
        )
    assert len(aliases["aliases"]) == 2


def test_registration_cannot_replace_swarm_git_identity(monkeypatch, tmp_path, repositories):
    first = repositories[0]
    config = tmp_path / "state" / "swarm" / "fixture" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"repo": str(first)}))
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / "state")
    outside = tmp_path / "outside"
    outside.mkdir()
    identity = project_identity.resolve_project(
        str(outside), {"AGENTIHOOKS_SWARM": "fixture", "AGENTIHOOKS_PROJECT_ID": "local:registered-project"}
    )
    assert identity.project_id == "github.com/first/common"


def test_alias_map_checks_all_types_before_following_chains(repositories):
    aliases = {
        "schema_version": "2.0",
        "aliases": {"github.com/first/common": "github.com/first/new", "github.com/first/new": []},
    }
    with pytest.raises(ValueError, match="^Invalid project alias map(: cycle)?$"):
        project_identity.resolve_project(str(repositories[0]), {}, aliases=aliases)


@pytest.mark.parametrize(
    "remote, expected",
    [
        ("https://GitLab.example/Owner/Repo.git", "gitlab.example/Owner/Repo"),
        ("ssh://gitlab.example/Owner/Repo.git", "gitlab.example/Owner/Repo"),
        ("git@gitlab.example:Owner/Repo.git", "gitlab.example/Owner/Repo"),
        ("https://github.com/first/common", "github.com/first/common"),
        ("https://github..com/first/common", "unknown"),
        ("https://github.com:443/first/common", "unknown"),
        ("ssh://alice@github.com/first/common", "unknown"),
    ],
)
def test_normalized_forge_host_and_case(remote, expected):
    assert project_identity.canonical_remote(remote) == expected


def test_metadata_schema_and_authority_boundary(repositories):
    schema = json.loads((Path(__file__).parents[2] / "docs/swarm-v2/schemas/project.json").read_text())
    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    metadata = project_identity.resolve_project(str(repositories[0]), {}).attributes()
    validator.validate(metadata)
    aliases = json.loads((Path(__file__).parents[1] / "fixtures/swarm_v2/project-identity.json").read_text())[
        "alias_map"
    ]
    validator.validate(aliases)
    with pytest.raises(ValidationError):
        validator.validate({**metadata, "grant_ref": "forged-display-grant"})
    assert "grant_ref" not in metadata


def test_git_registration_and_swarm_worktree_expand_tilde(monkeypatch, tmp_path, repositories):
    first, _, _, _, linked = repositories
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    identity = project_identity.resolve_project(
        "~/first/common", {"AGENTIHOOKS_PROJECT_ID": "local:registered-project"}
    )
    assert identity.project_id == "github.com/first/common"
    config = tmp_path / "state" / "swarm" / "fixture" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"repo": "~/first/common"}))
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / "state")
    identity = project_identity.resolve_project(str(linked), {"AGENTIHOOKS_SWARM": "fixture"})
    assert identity.project_id == "github.com/first/common"
    assert identity.worktree == "linked"


@pytest.mark.parametrize("remote", ["https://github.com/first/common.git", ""])
def test_git_observation_survives_failed_rediscovery(tmp_path, monkeypatch, remote):
    replies = iter([str(tmp_path / ".git"), str(tmp_path), remote, ""])
    monkeypatch.setattr(project_identity, "_git", lambda *args: next(replies))
    identity = project_identity.resolve_project(str(tmp_path), {"AGENTIHOOKS_PROJECT_ID": "local:registered"})
    assert identity.project_id == ("github.com/first/common" if remote else "unknown")
    assert next(replies) == ""


@pytest.mark.parametrize("registered", ["local:UpperCase", "local:" + "a" * 128])
def test_registration_preserves_valid_case_and_length(tmp_path, registered):
    assert (
        project_identity.resolve_project(str(tmp_path), {"AGENTIHOOKS_PROJECT_ID": registered}).project_id == registered
    )


def test_non_git_checkout_provenance_includes_nested_folder(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    inside = project_identity.ProjectIdentity("local", "local", cwd=str(nested))
    outside = project_identity.ProjectIdentity("local", "local", cwd=str(tmp_path.parent))
    assert project_identity._same_checkout(inside, str(tmp_path))
    assert not project_identity._same_checkout(outside, str(tmp_path))


@pytest.mark.parametrize("contents", ["{}", "{"])
def test_missing_swarm_repository_keeps_observed_git_identity(monkeypatch, tmp_path, repositories, contents):
    first = repositories[0]
    config = tmp_path / "state" / "swarm" / "fixture" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(contents)
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / "state")
    env = {"AGENTIHOOKS_SWARM": "fixture", "AGENTIHOOKS_PROJECT_ID": "local:registered"}
    assert project_identity.resolve_project(str(first), env).project_id == "github.com/first/common"


def test_nested_git_directory_shares_the_configured_checkout(repositories):
    first, _, _, nested, _ = repositories
    working = project_identity.resolve_project(str(nested), {})
    assert project_identity._same_checkout(working, str(first))


def test_absent_swarm_binding_does_not_read_swarm_configuration(monkeypatch, tmp_path):
    def refused(*args, **kwargs):
        raise AssertionError("Unbound resolver read swarm configuration")

    monkeypatch.setattr(Path, "read_text", refused)
    assert project_identity.resolve_project(str(tmp_path), {}) is None
