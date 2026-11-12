import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.swarm.health import corpus
from scripts.swarm.health import findings as health

CORPUS = Path(__file__).parent / "health_corpus"
OCTOBER = (
    "fresh engineer whose required comments were counted as talk",
    "Codex engineer whose pushes were never recorded on the ledger",
)


def cases() -> list[corpus.Case]:
    return corpus.load(CORPUS)


@pytest.mark.feature("swarm-health-corpus")
def test_every_detector_has_a_finding_case_and_a_none_case() -> None:
    assert {case.detector for case in cases()} == set(corpus.DETECTORS)
    assert corpus.unproven(cases()) == []


@pytest.mark.feature("swarm-health-corpus")
@pytest.mark.parametrize("case", cases(), ids=lambda case: f"{case.detector}: {case.name}")
def test_each_labelled_history_replays_to_its_label(case: corpus.Case) -> None:
    assert corpus.outcome(case, health.Limits()) == case.label


@pytest.mark.feature("swarm-health-corpus")
def test_the_ninth_of_october_ceremony_histories_raise_no_finding() -> None:
    october = [case for case in cases() if case.name in OCTOBER]
    assert [(case.detector, case.label) for case in october] == [("ceremony", "none")] * 2
    assert corpus.flips(october, health.Limits()) == []


@pytest.mark.feature("swarm-health-corpus")
def test_the_ninth_of_october_histories_are_red_when_talk_counts_are_dropped() -> None:
    october = [case for case in cases() if case.name in OCTOBER]
    dropped = [replace(case, history={**case.history, "talk": None}) for case in october]
    assert [corpus.outcome(case, health.Limits()) for case in dropped] == ["finding", "finding"]


@pytest.mark.feature("swarm-health-corpus")
def test_a_flipped_label_fails_the_replay(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for path in CORPUS.glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text())
    assert corpus.main([str(tmp_path)]) == 0
    planted = json.loads((tmp_path / "stale_claim.json").read_text())
    planted["cases"][0]["label"] = "none"
    (tmp_path / "stale_claim.json").write_text(json.dumps(planted))
    assert corpus.main([str(tmp_path)]) == 1
    assert "flipped  stale claim  claim with no ledger change for forty five minutes: labelled none" in (
        capsys.readouterr().out
    )


@pytest.mark.feature("swarm-health-corpus")
def test_a_detector_without_both_labels_fails_the_replay(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for path in CORPUS.glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text())
    (tmp_path / "over_monitoring.json").unlink()
    assert corpus.main([str(tmp_path)]) == 1
    assert "unproven  over monitoring: needs a finding case and a none case" in capsys.readouterr().out


def test_a_label_outside_finding_and_none_is_refused(tmp_path: Path) -> None:
    case = {"name": "odd", "label": "maybe", "history": {}}
    (tmp_path / "odd.json").write_text(json.dumps({"detector": "ceremony", "cases": [case]}))
    with pytest.raises(ValueError, match="odd is labelled maybe"):
        corpus.load(tmp_path)
