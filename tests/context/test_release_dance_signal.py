import json

import pytest

import hooks.context.branch_guard as branch_guard
import hooks.context.ci_manifesto as ci_manifesto
import hooks.context.prod_lockdown as prod_lockdown
from hooks.context.controls_toggle import set_controls_disabled
from hooks.hook_manager import BlockAction


def gh(argv, base="dev", queue=None):
    if argv[1:3] == ["api", "graphql"]:
        return json.dumps({"data": {"resource": {"repository": {"mergeQueue": queue}}}}), ""
    return f"{base} https://github.com/owner/repo/pull/123\n", ""


@pytest.fixture
def session(tmp_path, monkeypatch):
    for module in (branch_guard, prod_lockdown):
        monkeypatch.setattr(module, "get_redis", lambda: None)
        monkeypatch.setattr(module, "AGENTIHOOKS_HOME", tmp_path)
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, *gh(argv)))
    monkeypatch.setattr(ci_manifesto, "_manifesto_paths", lambda: [])
    return "release-publication"


def check(command, session, cwd=""):
    payload = {"tool_input": {"command": command}, "session_id": session, "cwd": cwd}
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
    check("gh pr merge 123 --squash", session)


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


@pytest.mark.parametrize("branch_command", ["git switch -c topic", "git checkout -b topic", "git branch topic"])
@pytest.mark.parametrize("signal", ["branch", "bypass"])
@pytest.mark.parametrize("base", ["main", "master", "v1"])
def test_branch_authorization_cannot_skip_protected_creation(session, branch_command, signal, base):
    if signal == "branch":
        branch_guard.set_branch_signal(session)
    else:
        set_controls_disabled(session)
    with pytest.raises(BlockAction) as blocked:
        check(f"{branch_command} && gh pr create --base {base}", session)
    assert str(blocked.value) == (
        f"BLOCKED: agent PR creation into '{base}' is forbidden. CI alone promotes dev into protected branches."
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


@pytest.mark.parametrize("flag", ["--title", "--body", "-t", "-b"])
@pytest.mark.parametrize("value", ["--base=dev", "--base dev", "-Bdev"])
def test_pr_message_cannot_change_the_actual_base(session, flag, value):
    with pytest.raises(BlockAction) as blocked:
        check(f"gh pr create --base main {flag} '{value}'", session)
    assert str(blocked.value) == (
        "BLOCKED: agent PR creation into 'main' is forbidden. CI alone promotes dev into protected branches."
    )


@pytest.mark.parametrize(
    ("command", "selector", "repo"),
    [
        ("gh pr merge 123 --rebase", "123", None),
        ("gh pr merge https://github.com/owner/repo/pull/123 --squash", "https://github.com/owner/repo/pull/123", None),
        ("gh pr merge --rebase", None, None),
        ("gh -Rowner/repo pr merge 123 --squash", "123", "owner/repo"),
        ("gh pr merge 123 --match-head-commit abcdef", "123", None),
        ("gh pr merge 123 --author-email author@example.com", "123", None),
        ("gh pr merge 123 -A author@example.com", "123", None),
        ("gh pr merge 123 --body-file message.txt", "123", None),
        ("gh pr merge 123 -F message.txt", "123", None),
        ("gh pr merge --subject 123 --squash", None, None),
        ("gh pr merge -t 123 -b 456 --squash", None, None),
        ("gh pr merge 123 --subject main --body master --squash", "123", None),
        ("gh -R owner/repo pr merge 123 --rebase", "123", "owner/repo"),
        ("gh pr merge --rebase 123 --repo=owner/repo", "123", "owner/repo"),
    ],
)
@pytest.mark.parametrize("base", ["main", "master", "v1", "dev"])
def test_merge_looks_up_the_actual_base(session, monkeypatch, tmp_path, command, selector, repo, base):
    import subprocess

    seen = []
    expected = ["gh", "pr", "view"]
    if selector:
        expected.append(selector)
    if repo:
        expected.extend(["--repo", repo])
    expected.extend(["--json", "baseRefName,url", "--jq", '.baseRefName + " " + .url'])

    def run(argv, **kwargs):
        seen.append(argv)
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 10, "cwd": str(tmp_path)}
        return subprocess.CompletedProcess(argv, 0, *gh(argv, base))

    monkeypatch.setattr(subprocess, "run", run)
    if base == "dev":
        check(command, session, str(tmp_path))
    else:
        with pytest.raises(BlockAction) as blocked:
            check(command, session, str(tmp_path))
        assert str(blocked.value) == (
            "BLOCKED [gh pr merge to main/master/v1]: CI alone promotes dev into protected branches."
        )
    assert seen[0] == expected
    assert len(seen) == (2 if base == "dev" else 1)


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


def test_merge_into_a_merge_queue_branch_is_refused(session, monkeypatch):
    import subprocess

    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, *gh(argv, queue={"id": "MQ"}))

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(BlockAction) as blocked:
        check("gh pr merge 123 --squash", session)
    assert str(blocked.value).startswith(
        "BLOCKED [gh pr merge into a merge queue branch]: 'dev' merges only through its merge queue"
    )
    assert "agentihooks swarm <slug> merge queue https://github.com/owner/repo/pull/123" in str(blocked.value)
    assert seen[1][:3] == ["gh", "api", "graphql"]
    assert "url=https://github.com/owner/repo/pull/123" in seen[1]
    assert "branch=dev" in seen[1]


def test_operator_bypass_lets_a_merge_skip_the_queue(session, monkeypatch):
    import subprocess

    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, *gh(argv, queue={"id": "MQ"}))

    monkeypatch.setattr(subprocess, "run", run)
    set_controls_disabled(session)
    check("gh pr merge 123 --squash", session)
    assert [argv[1:3] for argv in seen] == [["pr", "view"]]


def test_emergency_phrase_does_not_open_the_merge_queue(session, monkeypatch):
    import subprocess

    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, *gh(argv, queue={"id": "MQ"}))
    )
    prod_lockdown.set_bypass(session)
    with pytest.raises(BlockAction) as blocked:
        check("gh pr merge 123 --squash", session)
    assert str(blocked.value).startswith("BLOCKED [gh pr merge into a merge queue branch]")


@pytest.mark.parametrize("answer", ["failed", "{}", "not json", '{"data": {"resource": null}}'])
def test_unknown_merge_queue_keeps_the_merge_closed(session, monkeypatch, answer):
    import subprocess

    def run(argv, **kwargs):
        if argv[1:3] != ["api", "graphql"]:
            return subprocess.CompletedProcess(argv, 0, *gh(argv))
        if answer == "failed":
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, answer, "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(BlockAction) as blocked:
        check("gh pr merge 123 --squash", session)
    assert str(blocked.value) == "BLOCKED: Cannot tell whether 'dev' has a merge queue; the merge stays closed."


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


def test_legacy_custom_hotfix_vocabulary_is_preserved(session, tmp_path, monkeypatch):
    manifesto = tmp_path / "hotfix.md"
    manifesto.write_text("**Hotfix signals**\n```\nrepair production now\n```\n")
    monkeypatch.setattr(ci_manifesto, "_manifesto_paths", lambda: [manifesto])
    assert ci_manifesto.get_hotfix_signals() == ["repair production now"]


def test_invalid_merge_selector_stays_closed(session):
    with pytest.raises(BlockAction) as blocked:
        check("gh pr merge 123 456 --squash", session)
    assert str(blocked.value) == "BLOCKED: Cannot resolve the pull request base; promotion protection remains closed."


def test_absent_merge_cwd_uses_the_calling_directory(session, tmp_path, monkeypatch):
    import subprocess

    (tmp_path / "XXXX").mkdir()
    monkeypatch.chdir(tmp_path)
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs["cwd"])
        return subprocess.CompletedProcess(argv, 0, *gh(argv))

    monkeypatch.setattr(subprocess, "run", run)
    prod_lockdown.check_prod_lockdown({"tool_input": {"command": "gh pr merge 123 --squash"}, "session_id": session})
    assert seen == [str(tmp_path), str(tmp_path)]


def test_pr_refusal_preserves_the_full_diagnostic_and_log(session, monkeypatch):
    events = []
    monkeypatch.setattr(branch_guard, "log", lambda *args: events.append(args))
    command = "gh pr create --title " + "x" * 240
    with pytest.raises(BlockAction) as blocked:
        check(command, session)
    assert str(blocked.value) == (
        "BLOCKED: gh pr create needs an explicit --base.\n"
        "A bare `gh pr create` targets the default branch implicitly.\n"
        "Use --base dev for a worktree PR. CI alone opens promotion PRs."
    )
    assert events == [
        (
            "branch_guard: PR creation blocked (no explicit base — defaults to main)",
            {"command": "gh pr create --title " + "x" * 179, "session_id": "release-publication"},
        )
    ]


def test_dev_creation_keeps_its_diagnostic(session, monkeypatch):
    events = []
    monkeypatch.setattr(branch_guard, "log", lambda *args: events.append(args))
    check("gh pr create --base dev", session)
    assert events == [("branch_guard: PR into dev allowed", {"session_id": "release-publication"})]


def test_nonprotected_pr_signal_and_limit_are_session_scoped(session, monkeypatch):
    events = []
    monkeypatch.setattr(branch_guard, "log", lambda *args: events.append(args))
    branch_guard.set_pr_signal(session)
    for _ in range(3):
        check("gh pr create --base feature", session)
    with pytest.raises(BlockAction) as blocked:
        check("gh pr create --base feature", session)
    assert str(blocked.value) == (
        "BLOCKED: PR creation limit reached (3 PRs this session).\n"
        "Include a PR phrase in your next message to re-authorize and reset the counter."
    )
    assert events == [
        ("branch_guard: PR creation allowed", {"count": 1, "base": "feature", "session_id": "release-publication"}),
        ("branch_guard: PR creation allowed", {"count": 2, "base": "feature", "session_id": "release-publication"}),
        ("branch_guard: PR creation allowed", {"count": 3, "base": "feature", "session_id": "release-publication"}),
        ("branch_guard: PR creation blocked (counter limit)", {"count": 3, "session_id": "release-publication"}),
    ]
    set_controls_disabled(session)
    check("gh pr create --base feature", session)
    assert events[-1] == (
        "branch_guard: PR creation allowed",
        {"count": 4, "base": "feature", "session_id": "release-publication"},
    )


def test_nonprotected_pr_without_a_signal_keeps_its_diagnostic(session, monkeypatch):
    events = []
    monkeypatch.setattr(branch_guard, "log", lambda *args: events.append(args))
    command = "gh pr create --base feature --title " + "x" * 240
    with pytest.raises(BlockAction) as blocked:
        check(command, session)
    assert str(blocked.value) == (
        "BLOCKED: agent PR creation into 'feature' is disabled (CI Manifesto §15).\n"
        "PRs into dev need no signal; CI alone promotes into main, master and v1.\n"
        "To unlock for this session, the operator includes a PR phrase in a message\n"
        "(e.g. 'open a PR', 'create a PR', 'make a PR', 'pr please')."
    )
    assert events == [
        (
            "branch_guard: PR creation blocked (no signal)",
            {"command": command[:200], "base": "feature", "session_id": "release-publication"},
        )
    ]


def test_controls_failure_cannot_open_a_nonprotected_pr(session, monkeypatch):
    def unavailable(_):
        raise RuntimeError("controls unavailable")

    monkeypatch.setattr("hooks.context.controls_toggle.is_controls_disabled", unavailable)
    with pytest.raises(BlockAction) as blocked:
        check("gh pr create --base feature", session)
    assert str(blocked.value) == (
        "BLOCKED: agent PR creation into 'feature' is disabled (CI Manifesto §15).\n"
        "PRs into dev need no signal; CI alone promotes into main, master and v1.\n"
        "To unlock for this session, the operator includes a PR phrase in a message\n"
        "(e.g. 'open a PR', 'create a PR', 'make a PR', 'pr please')."
    )
