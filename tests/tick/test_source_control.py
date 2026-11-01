import importlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.tick.adapters import fakes
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Unsupported


@pytest.mark.parametrize("operation", ["enqueue", "queue_health"])
def test_fake_without_queue_reports_unsupported(operation: str) -> None:
    provider = fakes.FakeSourceControl()
    deadline = Deadline.after(10, clock=lambda: 0.0)
    method = getattr(provider, operation)
    args = ("https://example.test/project/change/7",) if operation == "enqueue" else ()

    assert method(*args, deadline=deadline) == Unsupported(operation)
    assert provider.calls[-1].operation == operation
    assert provider.calls[-1].args == args
    assert provider.calls[-1].deadline is deadline


def test_domain_types_preserve_provider_neutral_state() -> None:
    port = importlib.import_module("scripts.tick.ports.source_control")
    reference = port.ChangeRef("owner/project", 7)
    change = port.ChangeState(reference, "open", "head", "dev", False, "https://example.test/7")
    check = port.CheckState("lint", "completed", "success", "https://example.test/check/1")
    run = port.Run(19, "Tests", "head", "completed", "success", "https://example.test/run/19")

    assert change.reference == reference
    assert (change.state, change.head, change.base, change.merged, change.url) == (
        "open",
        "head",
        "dev",
        False,
        "https://example.test/7",
    )
    assert (check.name, check.status, check.conclusion, check.url) == (
        "lint",
        "completed",
        "success",
        "https://example.test/check/1",
    )
    assert (run.id, run.name, run.head, run.status, run.conclusion, run.url) == (
        19,
        "Tests",
        "head",
        "completed",
        "success",
        "https://example.test/run/19",
    )


def test_github_change_uses_audited_reads_and_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter = importlib.import_module("scripts.tick.adapters.github")
    port = importlib.import_module("scripts.tick.ports.source_control")
    calls: list[tuple[str, list[str], dict[str, object]]] = []

    def rest(caller: str, args: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append((caller, args, options))
        return subprocess.CompletedProcess(
            ["gh", *args],
            0,
            json.dumps(
                {
                    "number": 7,
                    "state": "open",
                    "head": {"sha": "head"},
                    "base": {"ref": "dev"},
                    "merged": False,
                    "html_url": "https://github.com/owner/project/pull/7",
                }
            ),
            "",
        )

    monkeypatch.setattr(adapter.github_reads, "rest", rest)
    deadline = Deadline.after(9, clock=lambda: 0.0)
    provider = adapter.GitHubSourceControl("owner/project", tmp_path)
    reference = port.ChangeRef("owner/project", 7)

    assert provider.change(reference, deadline=deadline) == port.ChangeState(
        reference, "open", "head", "dev", False, "https://github.com/owner/project/pull/7"
    )
    assert len(calls) == 1
    assert calls[0][1] == ["api", "repos/owner/project/pulls/7"]
    assert calls[0][2]["timeout"] == 9
    assert calls[0][2]["cwd"] == tmp_path


def test_github_expired_deadline_prevents_external_reads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter = importlib.import_module("scripts.tick.adapters.github")
    provider = adapter.GitHubSourceControl("owner/project", tmp_path)

    def refuse(*args: object, **kwargs: object) -> None:
        pytest.fail("expired deadline reached the provider")

    monkeypatch.setattr(adapter.github_reads, "rest", refuse)
    with pytest.raises(TimeoutError):
        provider.tags(deadline=Deadline(0, clock=lambda: 0.0))
