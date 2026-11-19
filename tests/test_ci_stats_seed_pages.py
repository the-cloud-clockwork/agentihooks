from pathlib import Path
from urllib.parse import parse_qs

import pytest

from scripts import ci_stats_seed as seed
from tests.test_ci_stats_seed import _archive

pytestmark = pytest.mark.unit

PULL = {
    "pulls/7": {"head": {"sha": "abc"}},
    f"{seed.TESTS}?event=pull_request&head_sha=abc&per_page=10": {"workflow_runs": [{"id": 1}]},
}


def _listing(monkeypatch: pytest.MonkeyPatch, artifacts: list[dict[str, object]], pages: list[int]) -> None:
    def api(path: str, binary: bool = False, deadline: float | None = None) -> object:
        if binary:
            return _archive({"x.index": "k"})
        if path in PULL:
            return PULL[path]
        listing, _, query = path.partition("?")
        assert listing == "actions/runs/1/artifacts"
        fields = parse_qs(query)
        size = min(int(fields["per_page"][0]), 100)
        page = int(fields.get("page", ["1"])[0])
        pages.append(page)
        return {"total_count": len(artifacts), "artifacts": artifacts[(page - 1) * size : page * size]}

    monkeypatch.setattr(seed, "api", api)


def _fillers(count: int) -> list[dict[str, object]]:
    return [{"id": 100 + number, "name": f"mutation-report-{number}", "expired": False} for number in range(count)]


@pytest.mark.feature("stats-seed-artifacts-past-the-first-page")
def test_harvest_finds_a_stats_artifact_past_the_first_artifact_page_of_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages: list[int] = []
    _listing(
        monkeypatch,
        [
            {"id": 4, "name": "mutation-test-stats-cache-0", "expired": False},
            *_fillers(99),
            {"id": 5, "name": "mutation-test-stats-cache-1", "expired": False},
        ],
        pages,
    )
    assert seed.harvest(7, tmp_path, set()) == 2
    assert (tmp_path / "x.index").read_text() == "k"
    assert pages == [1, 2]


@pytest.mark.feature("stats-seed-artifacts-past-the-first-page")
def test_harvest_lists_at_most_ten_artifact_pages_of_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pages: list[int] = []
    _listing(monkeypatch, _fillers(1100), pages)
    assert seed.harvest(7, tmp_path, set()) == 0
    assert pages == list(range(1, 11))
