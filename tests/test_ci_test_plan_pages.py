import pytest

from scripts import ci_test_plan
from tests.test_ci_test_plan import ACTIONS, LATER, REPOSITORY, _counted, _github, _manifest, _run

pytestmark = pytest.mark.unit


@pytest.mark.feature("test-planner-artifacts-past-the-first-page")
def test_a_dev_run_whose_coverage_shards_spill_onto_the_second_artifact_page_is_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _github(monkeypatch, [_run(2), _run(1)], manifests={2: _manifest(2), 1: _manifest(1)}, fillers={2: 90})
    requests = _counted(monkeypatch)
    found = ci_test_plan.find_record(REPOSITORY, LATER)
    assert found is not None
    assert found[0]["id"] == 2
    assert [endpoint for endpoint in requests if "/runs/2/artifacts" in endpoint] == [
        f"{ACTIONS}/runs/2/artifacts?per_page=100&page=1",
        f"{ACTIONS}/runs/2/artifacts?per_page=100&page=2",
    ]


@pytest.mark.feature("test-planner-artifacts-past-the-first-page")
def test_a_dev_run_whose_first_artifact_page_is_short_lists_no_second_page(monkeypatch: pytest.MonkeyPatch) -> None:
    _github(monkeypatch, [_run(1)], manifests={1: _manifest(1)})
    requests = _counted(monkeypatch)
    found = ci_test_plan.find_record(REPOSITORY, LATER)
    assert found is not None
    assert found[0]["id"] == 1
    assert sum("/runs/1/artifacts?" in endpoint for endpoint in requests) == 1


@pytest.mark.feature("test-planner-artifacts-past-the-first-page")
def test_a_dev_run_is_listed_to_its_second_artifact_page_and_no_further(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _github(monkeypatch, [_run(2), _run(1)], manifests={2: _manifest(2), 1: _manifest(1)}, fillers={2: 250})
    requests = _counted(monkeypatch)
    found = ci_test_plan.find_record(REPOSITORY, LATER)
    assert found is not None
    assert found[0]["id"] == 1
    assert [endpoint for endpoint in requests if "/runs/2/artifacts" in endpoint] == [
        f"{ACTIONS}/runs/2/artifacts?per_page=100&page=1",
        f"{ACTIONS}/runs/2/artifacts?per_page=100&page=2",
    ]
    assert capsys.readouterr().out == "Dev run 2 fills the 2 artifact pages the planner reads\n"


@pytest.mark.feature("test-planner-artifacts-past-the-first-page")
def test_every_artifact_page_counts_against_the_request_budget(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [_run(n) for n in range(200, 0, -1)]
    _github(
        monkeypatch,
        runs,
        manifests={run["id"]: _manifest(run["id"]) for run in runs},
        failed={(run["id"], "unit (3.12, 4)") for run in runs},
        fillers={run["id"]: 90 for run in runs},
    )
    requests = _counted(monkeypatch)
    assert ci_test_plan.find_record(REPOSITORY, LATER) is None
    assert len(requests) == 82 <= ci_test_plan.REQUESTS
    assert capsys.readouterr().out == (
        "Searched 20 dev push runs in 82 of 100 GitHub requests, request budget exhausted:"
        " 0 kept no manifest or shard coverage, 20 failed unit shards, 0 recorded another commit\n"
    )
