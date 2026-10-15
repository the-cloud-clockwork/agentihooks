import pytest

from hooks.context import branch_guard, ci_manifesto, prod_lockdown
from hooks.context.controls_toggle import set_controls_disabled
from hooks.hook_manager import BlockAction


@pytest.fixture
def session(tmp_path, monkeypatch):
    for module in (branch_guard, prod_lockdown):
        monkeypatch.setattr(module, "get_redis", lambda: None)
        monkeypatch.setattr(module, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(ci_manifesto, "_manifesto_paths", lambda: [])
    return "release-publication"


def check(command, session):
    payload = {"tool_input": {"command": command}, "session_id": session}
    branch_guard.check_branch_guard(payload)
    prod_lockdown.check_prod_lockdown(payload)


def test_release_dance_opens_publication_without_a_pr_signal(session):
    assert ci_manifesto.contains_release_signal("do the release dance")
    assert not ci_manifesto.contains_pr_signal("do the release dance")
    prod_lockdown.set_release_signal(session)
    check("gh workflow run publish-pypi.yml --ref main", session)


def test_publication_requires_release_dance(session):
    with pytest.raises(BlockAction) as blocked:
        check("gh workflow run publish-pypi.yml --ref main", session)
    assert str(blocked.value) == (
        "BLOCKED [PyPI publication]: release dance required — see CI Manifesto §4.1\n"
        "The operator must request the release dance."
    )


@pytest.mark.parametrize("prompt", ["release", "ship it", "merge to main", "merge it", "never release dance"])
def test_other_phrases_do_not_open_publication(session, prompt):
    assert not ci_manifesto.contains_release_signal(prompt)


@pytest.mark.parametrize("base", ["main", "master", "v1"])
@pytest.mark.parametrize("operation", ["create", "merge"])
@pytest.mark.parametrize("signal", ["none", "release", "hotfix", "pr", "bypass", "legacy"])
def test_protected_promotions_stay_closed(session, base, operation, signal):
    setters = {
        "none": lambda _: None,
        "release": prod_lockdown.set_release_signal,
        "hotfix": prod_lockdown.set_hotfix_signal,
        "pr": branch_guard.set_pr_signal,
        "bypass": set_controls_disabled,
        "legacy": prod_lockdown.set_bypass,
    }
    setters[signal](session)
    command = f"gh pr create --base {base}" if operation == "create" else f"gh pr merge 123 --base {base}"
    with pytest.raises(BlockAction) as blocked:
        check(command, session)
    assert str(blocked.value) == (
        f"BLOCKED: agent PR creation into '{base}' is forbidden. CI alone promotes dev into protected branches."
        if operation == "create"
        else "BLOCKED [gh pr merge to main/master/v1]: CI alone promotes dev into protected branches."
    )


@pytest.mark.parametrize("signal", [False, True])
def test_dev_pull_requests_remain_open(session, signal):
    if signal:
        prod_lockdown.set_release_signal(session)
        branch_guard.set_pr_signal(session)
    check("gh pr create --base dev --fill", session)
    check("gh pr merge 123 --base dev", session)


@pytest.mark.parametrize("signal", ["release", "hotfix", "bypass", "legacy"])
def test_release_workflow_stays_owned_by_ci(session, signal):
    setters = {
        "release": prod_lockdown.set_release_signal,
        "hotfix": prod_lockdown.set_hotfix_signal,
        "bypass": set_controls_disabled,
        "legacy": prod_lockdown.set_bypass,
    }
    setters[signal](session)
    with pytest.raises(BlockAction) as blocked:
        check("gh workflow run release.yml --ref main", session)
    assert str(blocked.value) == "BLOCKED [release.yml workflow trigger]: CI alone creates releases."


@pytest.mark.parametrize("separator", [";", "\n", "&&", "||", "|"])
@pytest.mark.parametrize("protected_first", [False, True])
def test_each_pr_creation_is_guarded(session, separator, protected_first):
    commands = ["gh pr create --base dev", "gh pr create --base main"]
    if protected_first:
        commands.reverse()
    with pytest.raises(BlockAction) as blocked:
        check(separator.join(commands), session)
    assert str(blocked.value) == (
        "BLOCKED: agent PR creation into 'main' is forbidden. CI alone promotes dev into protected branches."
    )


@pytest.mark.parametrize(
    "arguments",
    ["--base dev --base main", "--base=dev --base=main", "-B dev -B main"],
)
def test_effective_pr_base_is_protected(session, arguments):
    with pytest.raises(BlockAction) as blocked:
        check(f"gh pr create {arguments}", session)
    assert str(blocked.value) == (
        "BLOCKED: agent PR creation into 'main' is forbidden. CI alone promotes dev into protected branches."
    )


@pytest.mark.parametrize(
    ("command", "selector", "repo"),
    [
        ("gh pr merge 123 --rebase", "123", None),
        ("gh pr merge https://github.com/owner/repo/pull/123 --squash", "https://github.com/owner/repo/pull/123", None),
        ("gh pr merge --rebase", None, None),
        ("gh -R owner/repo pr merge 123 --rebase", "123", "owner/repo"),
        ("gh pr merge --rebase 123 --repo=owner/repo", "123", "owner/repo"),
    ],
)
@pytest.mark.parametrize("base", ["main", "master", "v1", "dev"])
def test_merge_looks_up_the_actual_base(session, monkeypatch, command, selector, repo, base):
    import subprocess

    seen = []
    expected = ["gh", "pr", "view"]
    if selector:
        expected.append(selector)
    if repo:
        expected.extend(["--repo", repo])
    expected.extend(["--json", "baseRefName", "--jq", ".baseRefName"])

    def run(argv, **kwargs):
        seen.append(argv)
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 10}
        return subprocess.CompletedProcess(argv, 0, f"{base}\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    if base == "dev":
        check(command, session)
    else:
        with pytest.raises(BlockAction) as blocked:
            check(command, session)
        assert str(blocked.value) == (
            "BLOCKED [gh pr merge to main/master/v1]: CI alone promotes dev into protected branches."
        )
    assert seen == [expected]


@pytest.mark.parametrize("failure", ["missing", "failed", "timeout", "empty"])
def test_unresolved_merge_stays_closed(session, monkeypatch, failure):
    import subprocess

    def run(argv, **kwargs):
        if failure == "missing":
            raise OSError("gh unavailable")
        if failure == "failed":
            raise subprocess.CalledProcessError(1, argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 10)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(BlockAction) as blocked:
        check("gh pr merge 123 --rebase", session)
    assert str(blocked.value) == "BLOCKED: Cannot resolve the pull request base; promotion protection remains closed."


def test_loaded_legacy_signals_cannot_expand_publication(session, tmp_path, monkeypatch):
    manifesto = tmp_path / "manifesto.md"
    manifesto.write_text(
        "**Release-gate signals**\n"
        "```\nship it\nrelease dance\n```\n"
        "## 15. PR Discipline\n### Unlock (per-turn operator signal)\n"
        "```\nopen a pr\nrelease dance\n```\n"
    )
    monkeypatch.setattr(ci_manifesto, "_manifesto_paths", lambda: [manifesto])
    assert ci_manifesto.get_release_signals() == ["release dance"]
    assert not ci_manifesto.contains_release_signal("ship it")
    assert ci_manifesto.contains_release_signal("release dance")
    assert not ci_manifesto.contains_pr_signal("release dance")
