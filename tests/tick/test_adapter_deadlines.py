"""Deadline coverage of the tick adapters: every method taking a deadline keyword checks it before its I/O."""

import ast
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import fleet_red, github_reads
from scripts.tick.adapters.github import GitHubSourceControl
from scripts.tick.adapters.host import JobHost
from scripts.tick.adapters.inbox import StoreInbox
from scripts.tick.adapters.llm import ApiLlm
from scripts.tick.adapters.metrics import SpoolMetrics
from scripts.tick.adapters.processes import PassProcesses
from scripts.tick.adapters.runtime import RouterRuntime
from scripts.tick.deadline import Deadline
from scripts.tick.ports.source_control import ChangeRef

ADAPTERS = Path(__file__).resolve().parents[2] / "scripts" / "tick" / "adapters"
SKIPPED = {"__init__.py", "fakes.py"}
NO_DEADLINE = "records_redis.py"
REF = ChangeRef("o/r", 7)


def _first_statement(method: ast.FunctionDef) -> ast.stmt | None:
    """Return the first statement of the method body after its docstring and local imports."""
    body = [
        statement
        for statement in method.body
        if not isinstance(statement, (ast.Import, ast.ImportFrom))
        and not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
    ]
    return body[0] if body else None


def _checks(method: ast.FunctionDef) -> bool:
    """Say whether the method's first statement calls live on its deadline argument, before any other work."""
    first = _first_statement(method)
    return first is not None and any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "live"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "deadline"
        for node in ast.walk(first)
    )


def coverage(source: str) -> tuple[int, list[str]]:
    """Count the methods taking a deadline keyword and name those that never call deadline.live()."""
    methods = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and any(arg.arg == "deadline" for arg in node.args.kwonlyargs)
    ]
    return len(methods), [method.name for method in methods if not _checks(method)]


def _modules() -> list[Path]:
    """List every adapter module the coverage walk grades."""
    return sorted(path for path in ADAPTERS.glob("*.py") if path.name not in SKIPPED)


@pytest.mark.skipif("MUTANT_UNDER_TEST" in os.environ, reason="the mutated copy rewrites every method body")
def test_every_adapter_method_checks_its_deadline() -> None:
    report: list[str] = []
    missing: list[str] = []
    for path in _modules():
        total, unchecked = coverage(path.read_text())
        report.append(f"{path.stem}: {total} methods, {total - len(unchecked)} checks")
        missing.extend(f"{path.stem}.{name}" for name in unchecked)
    print("\n".join(report))
    assert missing == [], "\n".join([*report, f"unchecked: {', '.join(missing)}"])


def test_the_walk_grades_the_adapter_modules() -> None:
    names = {path.name for path in _modules()}
    assert {"github.py", "runtime.py", "host.py", "metrics.py", "inbox.py", NO_DEADLINE} <= names
    assert not names & SKIPPED


def test_a_planted_method_without_a_check_is_named() -> None:
    source = (
        "class Port:\n"
        "    def planted(self, *, deadline):\n"
        "        return 1\n"
        "    def kept(self, *, deadline):\n"
        "        deadline.live()\n"
        "    def positional(self, deadline):\n"
        "        return 2\n"
    )
    assert coverage(source) == (2, ["planted"])


def test_a_check_after_other_work_is_not_a_check() -> None:
    source = (
        "def late(*, deadline):\n"
        '    """Docstring."""\n'
        "    call()\n"
        "    deadline.live()\n"
        "def imported(*, deadline):\n"
        '    """Docstring."""\n'
        "    from x import y\n"
        "    return y(deadline.live())\n"
        "def empty(*, deadline):\n"
        '    """Docstring."""\n'
    )
    assert coverage(source) == (3, ["late", "empty"])


def test_live_on_another_name_is_not_a_check() -> None:
    source = "def read(*, deadline, other):\n    other.live()\n    deadline.remaining\n"
    assert coverage(source) == (1, ["read"])


def test_the_records_adapter_takes_no_deadline() -> None:
    assert coverage((ADAPTERS / NO_DEADLINE).read_text()) == (0, [])


class _Boom:
    """Stand in for every outside system and fail the test on any touch."""

    def __call__(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("an adapter touched its outside system after its deadline expired")

    def __getattr__(self, name: str) -> Any:
        return self


EXPIRED = Deadline(1.0, clock=lambda: 1.0)


def _github(monkeypatch: pytest.MonkeyPatch) -> GitHubSourceControl:
    monkeypatch.setattr(github_reads, "rest", _Boom())
    monkeypatch.setattr(github_reads, "charge", _Boom())
    return GitHubSourceControl("o/r", Path("."), "dev", git=_Boom(), gh=_Boom())


GITHUB_CALLS: dict[str, Callable[[GitHubSourceControl], object]] = {
    "change": lambda port: port.change(REF, deadline=EXPIRED),
    "checks": lambda port: port.checks(REF, deadline=EXPIRED),
    "runs": lambda port: port.runs(REF, deadline=EXPIRED),
    "enqueue": lambda port: port.enqueue(REF, deadline=EXPIRED),
    "merge": lambda port: port.merge(REF, deadline=EXPIRED),
    "queue_health": lambda port: port.queue_health(deadline=EXPIRED),
    "tags": lambda port: port.tags(deadline=EXPIRED),
    "comment": lambda port: port.comment(REF, "body", deadline=EXPIRED),
    "capabilities": lambda port: port.capabilities(deadline=EXPIRED),
    "issue_state": lambda port: port.issue_state(REF, deadline=EXPIRED),
    "close_issue": lambda port: port.close_issue(REF, deadline=EXPIRED),
    "rate_limit": lambda port: port.rate_limit(deadline=EXPIRED),
    "issues_enabled": lambda port: port.issues_enabled(deadline=EXPIRED),
    "latest_runs": lambda port: port.latest_runs(deadline=EXPIRED),
    "cancel_run": lambda port: port.cancel_run(1, deadline=EXPIRED),
    "merged_at": lambda port: port.merged_at("abc", deadline=EXPIRED),
    "open_changes": lambda port: port.open_changes(deadline=EXPIRED),
    "workflow_runs": lambda port: port.workflow_runs("w.yml", "push", "failure", "2026-11-04", deadline=EXPIRED),
    "download": lambda port: port.download("path", deadline=EXPIRED),
    "graph": lambda port: port.graph("query", {}, deadline=EXPIRED),
}


@pytest.mark.parametrize("name", sorted(GITHUB_CALLS))
def test_github_refuses_an_expired_deadline_before_its_call(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(TimeoutError) as raised:
        GITHUB_CALLS[name](_github(monkeypatch))
    assert str(raised.value) == "tool deadline expired"


BOOM: Any = _Boom()


OTHER_CALLS: dict[str, Callable[[], object]] = {
    "runtime.inspect": lambda: RouterRuntime(BOOM, BOOM, BOOM, BOOM).inspect("a", deadline=EXPIRED),
    "runtime.launch": lambda: RouterRuntime(BOOM, BOOM, BOOM, BOOM).launch({}, deadline=EXPIRED),
    "runtime.stop": lambda: RouterRuntime(BOOM, BOOM, BOOM, BOOM).stop("a", deadline=EXPIRED),
    "runtime.panes": lambda: RouterRuntime(BOOM, BOOM, BOOM, BOOM).panes(deadline=EXPIRED),
    "inbox.read": lambda: StoreInbox(BOOM, "swarm").read("a", deadline=EXPIRED),
    "inbox.send": lambda: StoreInbox(BOOM, "swarm").send("a", "b", deadline=EXPIRED),
    "inbox.close": lambda: StoreInbox(BOOM, "swarm").close("i", "done", deadline=EXPIRED),
    "host.read": lambda: JobHost({"x": BOOM}, {}).read("x", deadline=EXPIRED),
    "host.run": lambda: JobHost({}, {"x": BOOM}).run({"job": "x"}, deadline=EXPIRED),
    "metrics.emit": lambda: SpoolMetrics(BOOM, {}).emit({}, deadline=EXPIRED),
    "llm.classify": lambda: ApiLlm("m", "u", BOOM).classify({}, deadline=EXPIRED),
    "processes.scan": lambda: PassProcesses(BOOM).scan(deadline=EXPIRED),
    "processes.rescan": lambda: PassProcesses(BOOM).rescan(deadline=EXPIRED),
}


@pytest.mark.parametrize("name", sorted(OTHER_CALLS))
def test_adapters_refuse_an_expired_deadline_before_their_call(name: str) -> None:
    with pytest.raises(TimeoutError) as raised:
        OTHER_CALLS[name]()
    assert str(raised.value) == "tool deadline expired"


LIVE = Deadline(10.0, clock=lambda: 4.0)


class _Done:
    """A finished command answering an empty JSON object."""

    stdout = "{}"
    returncode = 0

    def check_returncode(self) -> None:
        """Pass, as a finished command does."""


def _timeouts(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record the timeout of every audited gh call and charge nothing."""
    seen: list[object] = []

    def rest(caller: str, args: list[str], run: object = None, **options: object) -> _Done:
        seen.append(options["timeout"])
        return _Done()

    monkeypatch.setattr(github_reads, "rest", rest)
    monkeypatch.setattr(github_reads, "charge", lambda *args: None)
    return seen


def test_github_graph_bounds_its_call_by_the_live_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _timeouts(monkeypatch)
    assert GitHubSourceControl("o/r", Path("."), "dev").graph("query", {}, deadline=LIVE) == {}
    assert seen == [6.0]


def test_github_download_bounds_its_call_by_the_live_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _timeouts(monkeypatch)
    assert GitHubSourceControl("o/r", Path("."), "dev").download("path", deadline=LIVE) == "{}"
    assert seen == [6.0]


def test_github_api_calls_are_bounded_by_the_live_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _timeouts(monkeypatch)
    GitHubSourceControl("o/r", Path("."), "dev").cancel_run(5, deadline=LIVE)
    assert seen == [6.0]


def test_github_tags_bounds_git_by_the_live_seconds() -> None:
    seen: list[object] = []

    def git(args: list[str], **options: object) -> _Done:
        seen.append(options["timeout"])
        return _Done()

    port = GitHubSourceControl("o/r", Path("."), "dev", git=git)  # pyright: ignore[reportArgumentType]
    port.tags(deadline=LIVE)
    assert seen == [6.0]


def test_llm_classify_gives_the_backend_the_live_seconds() -> None:
    built: list[tuple[str, str, float]] = []

    class _Decider:
        def decide(self, request: object) -> str:
            return "decided"

    def backend(model: str, url: str, timeout: float) -> _Decider:
        built.append((model, url, timeout))
        return _Decider()

    request = {"state": {}, "questions": {}}
    assert ApiLlm("m", "u", backend).classify(request, deadline=LIVE) == "decided"
    assert built == [("m", "u", 6.0)]


class _Listing:
    """A source control port recording the deadline its open pull request listing was given."""

    def __init__(self) -> None:
        self.deadlines: list[Deadline] = []

    def open_changes(self, *, deadline: Deadline) -> tuple[()]:
        self.deadlines.append(deadline)
        return ()


def _read(deadline: Deadline | None) -> Deadline:
    port = _Listing()
    repos: list[str] = []

    def ports(repo: str) -> _Listing:
        repos.append(repo)
        return port

    assert fleet_red.read("o/r", ports, deadline=deadline) == ()  # pyright: ignore[reportArgumentType]
    assert repos == ["o/r"]
    return port.deadlines[0]


def test_fleet_red_read_keeps_a_caller_deadline_shorter_than_its_own() -> None:
    assert _read(Deadline(5.0, clock=lambda: 1.0)).expires_at == 5.0


def test_fleet_red_read_caps_a_long_caller_deadline_at_its_own() -> None:
    assert _read(Deadline(1000.0, clock=lambda: 1.0)).expires_at == 1.0 + fleet_red.SECONDS


def test_fleet_red_read_without_a_caller_deadline_gives_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[float] = []

    def after(seconds: float) -> Deadline:
        asked.append(seconds)
        return Deadline(3.0 + seconds, clock=lambda: 3.0)

    monkeypatch.setattr(fleet_red.Deadline, "after", after)
    assert _read(None).expires_at == 3.0 + fleet_red.SECONDS
    assert asked == [fleet_red.SECONDS]
