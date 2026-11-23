"""The GitHub open pull request listing against a recorded agentihooks listing served by a fake GitHub."""

import json
import re
import subprocess
import threading
from pathlib import Path

import pytest

from scripts.swarm import fleet_red
from scripts.tick.adapters import github
from scripts.tick.deadline import Deadline
from scripts.tick.ports.source_control import OpenChange, RollupCheck

RECORDED = Path(__file__).parents[1] / "fixtures" / "fleet_red_recorded_listing.json"
REPOSITORY = "the-cloud-clockwork/agentihooks"
PAGE = re.compile(r"pullRequests\(states: OPEN, first: (\d+)")
ALIAS = re.compile(r"(\w+): pullRequest\(number: (\d+)\)")
ROLLUP = ["commits", "nodes", 0, "commit", "statusCheckRollup"]


def _recorded() -> tuple[tuple[OpenChange, ...], int]:
    """Return the recorded open listing and the time it was recorded at, in milliseconds."""
    body = json.loads(RECORDED.read_text())
    pulls = tuple(OpenChange(url, tuple(RollupCheck(*check) for check in checks)) for url, checks in body["pulls"])
    return pulls, body["now_ms"]


def _number(url: str) -> int:
    """Return the number of the pull request at the url."""
    return int(url.rsplit("/", 1)[1])


class GitHub:
    """A rest stand in answering paged open listings and numbered rollup batches from recorded pull requests."""

    def __init__(self, pulls: tuple[OpenChange, ...], hidden: frozenset[int] = frozenset(), wait: float = 0.0) -> None:
        self.pulls = pulls
        self.numbered = {_number(pull.url): pull for pull in pulls}
        self.hidden = hidden
        self.wait = wait
        self.flying = 0
        self.most = 0
        self.turn = threading.Condition()

    def __call__(self, caller: str, args: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        query = next(arg.removeprefix("query=") for arg in args if arg.startswith("query="))
        after = next((arg.removeprefix("after=") for arg in args if arg.startswith("after=")), "0")
        if "statusCheckRollup" not in query:
            return self._answer(args, self._page(query, int(after)))
        with self.turn:
            self.flying += 1
            self.most = max(self.most, self.flying)
            self.turn.notify_all()
            self.turn.wait_for(lambda: self.flying > 1, timeout=self.wait)
        try:
            found = self._page(query, int(after)) if PAGE.search(query) else self._batch(query)
            return self._answer(args, found)
        finally:
            with self.turn:
                self.flying -= 1

    def _node(self, pull: OpenChange) -> dict[str, object]:
        """Return the GraphQL node of a recorded pull request, its rollup withheld when it is hidden."""
        contexts = [
            {
                "__typename": "CheckRun",
                "name": check.name,
                "conclusion": check.result,
                "startedAt": check.at,
                "completedAt": check.at,
            }
            for check in pull.checks
        ]
        rollup = None if _number(pull.url) in self.hidden else {"contexts": {"nodes": contexts}}
        commits = {"nodes": [{"commit": {"statusCheckRollup": rollup}}]}
        return {"url": pull.url, "number": _number(pull.url), "commits": commits}

    def _page(self, query: str, start: int) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Return one page of the open listing starting at the cursor, with an error per hidden rollup on it."""
        size = int(PAGE.findall(query)[0])
        listed = self.pulls[start : start + size]
        more = start + size < len(self.pulls)
        info = {"hasNextPage": more, "endCursor": str(start + size) if more else None}
        errors = [
            {"type": "FORBIDDEN", "path": ["repository", "pullRequests", "nodes", index, *ROLLUP]}
            for index, pull in enumerate(listed)
            if _number(pull.url) in self.hidden and "statusCheckRollup" in query
        ]
        return {"pullRequests": {"pageInfo": info, "nodes": [self._node(pull) for pull in listed]}}, errors

    def _batch(self, query: str) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Return each aliased pull request of a numbered batch, with an error per hidden rollup in it."""
        aliases = [(alias, int(number)) for alias, number in ALIAS.findall(query)]
        errors = [
            {"type": "FORBIDDEN", "path": ["repository", alias, *ROLLUP]} for alias, n in aliases if n in self.hidden
        ]
        return {alias: self._node(self.numbered[number]) for alias, number in aliases}, errors

    def _answer(
        self, args: list[str], found: tuple[dict[str, object], list[dict[str, object]]]
    ) -> subprocess.CompletedProcess[str]:
        """Answer with the repository data, refusing the call the way gh does when GraphQL reports errors."""
        repository, errors = found
        body = {"data": {"repository": repository}, **({"errors": errors} if errors else {})}
        if errors:
            raise subprocess.CalledProcessError(1, ["gh", *args], json.dumps(body), "gh: Resource not accessible")
        return subprocess.CompletedProcess(["gh", *args], 0, json.dumps(body), "")


def _listed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake: GitHub) -> tuple[OpenChange, ...]:
    """Return the adapter's open listing with every gh call answered by the fake GitHub."""
    monkeypatch.setattr(github.github_reads, "rest", fake)
    return github.GitHubSourceControl(REPOSITORY, tmp_path, "dev").open_changes(deadline=Deadline.after(60))


@pytest.mark.feature("fleet-red-open-listing-batches")
def test_the_open_listing_returns_the_recorded_listing_and_its_verdicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded, now = _recorded()

    listed = _listed(monkeypatch, tmp_path, GitHub(recorded))

    assert listed == recorded
    for pull in recorded:
        assert fleet_red.held(listed, pull.url, now) is fleet_red.held(recorded, pull.url, now)
        assert fleet_red.shared(listed, pull.url, now) == fleet_red.shared(recorded, pull.url, now)


@pytest.mark.feature("fleet-red-open-listing-batches")
def test_the_open_listing_reads_its_rollups_concurrently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorded = _recorded()[0][:60]
    fake = GitHub(recorded, wait=2.0)

    assert _listed(monkeypatch, tmp_path, fake) == recorded
    assert fake.most > 1


@pytest.mark.feature("fleet-red-open-listing-batches")
def test_a_rollup_the_app_cannot_read_lists_the_pull_request_with_no_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = _recorded()[0]
    hidden = frozenset({_number(recorded[3].url), _number(recorded[40].url)})

    listed = _listed(monkeypatch, tmp_path, GitHub(recorded, hidden))

    unchecked = {_number(pull.url) for pull in listed if not pull.checks} - {
        _number(pull.url) for pull in recorded if not pull.checks
    }
    assert unchecked == hidden
    assert [pull.url for pull in listed] == [pull.url for pull in recorded]
    assert [pull for pull in listed if _number(pull.url) not in hidden] == [
        pull for pull in recorded if _number(pull.url) not in hidden
    ]
