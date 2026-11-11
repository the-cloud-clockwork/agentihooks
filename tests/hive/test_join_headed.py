import subprocess
from types import SimpleNamespace
from typing import Any, cast

import pytest

HEADED = {"kubectl": True, "git_identity": True}


def passing() -> dict[str, bool]:
    from scripts.hive import join_report

    return dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)


@pytest.mark.feature("hive-headed-member-check")
@pytest.mark.parametrize("missing", ["kubectl", "git_identity"])
def test_a_headed_hive_without_kubectl_or_git_identity_grades_failed(missing: str) -> None:
    from scripts.hive import join_report

    checks = {**passing(), **HEADED, missing: False}
    report = join_report.graded({"checks": checks, "context": {"headed": True}})
    assert report["grade"] == "failed"
    assert report["misses"] == {missing: {"required": True}}


@pytest.mark.feature("hive-headed-member-check")
def test_a_headed_hive_with_kubectl_and_git_identity_grades_complete() -> None:
    from scripts.hive import join_report

    report = join_report.graded({"checks": {**passing(), **HEADED}, "context": {"headed": True}})
    assert report["grade"] == "complete"
    assert report["misses"] == {}


@pytest.mark.feature("hive-headed-member-check")
def test_a_headless_hive_is_not_graded_on_kubectl_or_git_identity() -> None:
    from scripts.hive import join_report

    report = join_report.graded({"checks": passing(), "context": {"headed": False}})
    assert report["grade"] == "complete"
    assert join_report.graded({"checks": passing()})["grade"] == "complete"


def _runs(monkeypatch: pytest.MonkeyPatch, answers: dict[tuple[str, ...], tuple[int, str]]) -> list[list[str]]:
    from scripts.hive import join_checks

    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": join_checks.COMMAND_TIMEOUT_S,
            "check": False,
        }
        code, out = answers[tuple(argv)]
        return SimpleNamespace(returncode=code, stdout=out)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


KUBECTL = ("kubectl", "get", "nodes", "--request-timeout=10s", "-o", "name")
NAME = ("git", "config", "--global", "user.name")
EMAIL = ("git", "config", "--global", "user.email")


@pytest.mark.feature("hive-headed-member-check")
@pytest.mark.parametrize(
    ("answer", "ready"),
    [((0, "node/master\n"), True), ((0, ""), False), ((1, ""), False)],
)
def test_kubectl_is_ready_only_when_it_lists_the_cluster_nodes(
    monkeypatch: pytest.MonkeyPatch, answer: tuple[int, str], ready: bool
) -> None:
    from scripts.hive import join_checks

    calls = _runs(monkeypatch, {KUBECTL: answer})
    assert join_checks.kubectl_ready() is ready
    assert calls == [list(KUBECTL)]


@pytest.mark.feature("hive-headed-member-check")
def test_kubectl_missing_from_the_hive_fails_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hive import join_checks

    def missing(argv: list[str], **kwargs: object) -> None:
        raise FileNotFoundError("kubectl")

    monkeypatch.setattr(subprocess, "run", missing)
    assert join_checks.checked(join_checks.kubectl_ready) is False


@pytest.mark.feature("hive-headed-member-check")
@pytest.mark.parametrize(
    ("name", "email", "ready"),
    [
        ((0, "agentihooks hive\n"), (0, "agentihooks-hive@users.noreply.github.com\n"), True),
        ((0, "agentihooks hive\n"), (1, ""), False),
        ((1, ""), (0, "agentihooks-hive@users.noreply.github.com\n"), False),
        ((0, "  \n"), (0, "agentihooks-hive@users.noreply.github.com\n"), False),
    ],
)
def test_git_identity_needs_both_a_global_name_and_email(
    monkeypatch: pytest.MonkeyPatch, name: tuple[int, str], email: tuple[int, str], ready: bool
) -> None:
    from scripts.hive import join_checks

    _runs(monkeypatch, {NAME: name, EMAIL: email})
    assert join_checks.git_identity_ready() is ready


@pytest.mark.feature("hive-headed-member-check")
@pytest.mark.parametrize("headed", [True, False])
def test_startup_runs_the_headed_checks_only_on_a_headed_hive(monkeypatch: pytest.MonkeyPatch, headed: bool) -> None:
    from scripts.hive import join_checks

    monkeypatch.setattr(join_checks, "cheap", lambda redis, report: {"pane": True})
    monkeypatch.setattr(join_checks, "ledger_checks", lambda report: {"ledger_join": True})
    monkeypatch.setattr(join_checks, "github_ready", lambda: True)
    monkeypatch.setattr(join_checks, "kubectl_ready", lambda: True)
    monkeypatch.setattr(join_checks, "git_identity_ready", lambda: False)
    checks = join_checks.startup(cast(Any, object()), {"context": {"headed": headed}})
    expected = {"pane": True, "ledger_join": True, "github": True}
    assert checks == ({**expected, "kubectl": True, "git_identity": False} if headed else expected)


@pytest.mark.feature("hive-headed-member-check")
@pytest.mark.parametrize(("record", "headed"), [({"ui": "yes"}, True), ({"ui": "no"}, False), (None, False)])
def test_a_hive_is_headed_when_its_registry_record_carries_the_interface(
    monkeypatch: pytest.MonkeyPatch, record: dict[str, str] | None, headed: bool
) -> None:
    from scripts.hive import join_checks, registry

    seen: list[tuple[object, str]] = []

    def show(client: object, hive_id: str) -> dict[str, str] | None:
        seen.append((client, hive_id))
        return record

    monkeypatch.setattr(registry, "show", show)
    redis = cast(Any, object())
    assert join_checks.headed(redis, "hive-1") is headed
    assert seen == [(redis, "hive-1")]
