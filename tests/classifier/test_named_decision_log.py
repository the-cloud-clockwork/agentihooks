import pytest

from hooks.classifier import api, decision_log, runner
from hooks.classifier.errors import ClassifierUnavailable

from .fakes import KEY, FakeUrlopen, ok
from .test_definitions import definition_home as definition_home
from .test_definitions import sample, write_definition


def test_named_run_logs_definition_digest_and_expected_label(definition_home, monkeypatch):
    write_definition(definition_home, sample())
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_URL", "https://classifier.invalid")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LITELLM_KEY", KEY)
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", "liquid-d1")
    wire = FakeUrlopen({"liquid-d1": ok({"answers": {"accept": {"type": "noul", "noul": 0.8}}})})
    monkeypatch.setattr(api, "urlopen", wire)
    with decision_log.record_context(expected={"accept": True}):
        output = runner.run("sample", {"input": 7})
    (row,) = decision_log.read()
    assert output.verdicts == {"accept": True}
    assert row["definition"] == "sample"
    assert row["definition_digest"] == output.definition.digest
    assert row["expected"] == {"accept": True}
    assert row["purpose"] == "sample"
    assert row["source"] == "liquid-d1"
    assert len(wire.calls) == 1
    decision_log.append("plain", None, None, 0)
    plain = decision_log.read()[-1]
    assert (plain["definition"], plain["definition_digest"], plain["expected"]) == (None, None, None)


def test_failed_named_run_logs_provenance_and_restores_context(definition_home, monkeypatch):
    write_definition(definition_home, sample())
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL", raising=False)
    with decision_log.record_context(expected=False):
        with pytest.raises(ClassifierUnavailable):
            runner.run("sample", "input")
    (row,) = decision_log.read()
    assert row["definition"] == "sample"
    assert len(row["definition_digest"]) == 64
    assert row["expected"] is False
    assert row["source"] is None
    decision_log.append("plain", None, None, 0)
    assert decision_log.read()[-1]["expected"] is None


def test_expected_label_context_supports_nested_null_and_restoration():
    with decision_log.record_context(expected="outer", definition="outer", definition_digest="digest"):
        with decision_log.record_context(expected=None):
            decision_log.append("nested", None, None, 0)
        decision_log.append("outer", None, None, 0)
    decision_log.append("plain", None, None, 0)
    rows = decision_log.read()
    assert [(row["definition"], row["definition_digest"], row["expected"]) for row in rows] == [
        ("outer", "digest", None),
        ("outer", "digest", "outer"),
        (None, None, None),
    ]
