from pathlib import Path

import pytest
import yaml

from scripts.ci_mutation.arrival import main, missing_producers

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _collected(folder: Path, *parts: int) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for part in parts:
        (folder / f"collected-{part}").touch()
    return folder


def test_every_producer_whose_marker_is_absent_is_named(tmp_path: Path) -> None:
    folder = _collected(tmp_path / "stats", 0, 2)
    assert missing_producers(folder, [0, 1, 2, 3]) == ["mutation-stats (1)", "mutation-stats (3)"]
    assert missing_producers(_collected(tmp_path / "full", 0, 1), [0, 1]) == []


def test_an_empty_download_names_every_producer_and_writes_the_gap_to_the_job_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert main([str(tmp_path / "absent"), "[0, 1]"]) == 0
    assert output.read_text() == "missing=mutation-stats (0), mutation-stats (1)\n"
    assert capsys.readouterr().out == (
        "::warning::mutation stats producer error: no part from mutation-stats (0), mutation-stats (1)"
        f" reached {tmp_path / 'absent'}; downloading the shared mutation stats again\n"
    )


def test_a_part_still_missing_is_a_producer_error_naming_the_producer_never_a_graded_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    folder = _collected(tmp_path / "stats", 0, 1, 2, 3, 4)
    assert main([str(folder), "[0, 1, 2, 3, 4, 5]", "--required"]) == 1
    assert capsys.readouterr().out == (
        f"::error::mutation stats producer error: no part from mutation-stats (5) reached {folder};"
        " the shard grades nothing without it\n"
    )


def test_every_arrived_part_passes_and_leaves_no_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert main([str(_collected(tmp_path / "stats", 0)), "[0]", "--required"]) == 0
    assert output.read_text() == "missing=\n"
    assert capsys.readouterr().out == "Every mutation stats producer's part arrived: 1 of 1\n"


def test_the_shard_downloads_a_missing_part_again_and_fails_on_its_producer_before_mutating() -> None:
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["mutation-shard"]["steps"]
    names = [step.get("name") for step in steps]
    check = steps[names.index("Check every mutation stats producer's part arrived")]
    again = steps[names.index("Download the missing mutation stats parts")]
    required = steps[names.index("Fail on a mutation stats producer whose part never arrived")]
    assert (
        names.index("Download the shared mutation stats, attempt 2")
        < names.index(check["name"])
        < names.index(again["name"])
        < names.index("Download the missing mutation stats parts, attempt 2")
        < names.index(required["name"])
        < names.index("Mutate changed Python files")
    )
    parts = "${{ needs.mutation-plan.outputs.stats_parts }}"
    assert check["env"]["STATS_PARTS"] == required["env"]["STATS_PARTS"] == parts
    assert check["run"] == 'python -m scripts.ci_mutation.arrival .mutation-stats "$STATS_PARTS"'
    assert required["run"] == 'python -m scripts.ci_mutation.arrival .mutation-stats "$STATS_PARTS" --required'
    assert check["id"] == "stats-arrival"
    assert again["if"] == "steps.stats-arrival.outputs.missing != ''"
    assert required["if"] == again["if"]
    assert again["with"] == steps[names.index("Download the shared mutation stats")]["with"]
