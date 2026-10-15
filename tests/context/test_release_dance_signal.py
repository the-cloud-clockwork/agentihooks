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
