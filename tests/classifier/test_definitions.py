from unittest.mock import patch

import pytest
import yaml

from hooks import config
from hooks.context import profile_chain


@pytest.fixture
def definition_home(tmp_path, monkeypatch):
    package = tmp_path / "profiles"
    home = tmp_path / "home"
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", package)
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", home)
    with patch.object(profile_chain, "read_state", return_value={}):
        yield package / "package" / "classifiers"


def write_definition(folder, raw):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "sample.yaml").write_text(yaml.safe_dump(raw))


def sample():
    return {
        "version": 1,
        "purpose": "sample",
        "fallbacks": "none",
        "questions": [{"name": "accept", "type": "yesno", "instructions": "Accept?"}],
        "thresholds": {"yes": 0.6},
        "rule": {"type": "yes", "threshold": "yes"},
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "questions",
            [{"name": "accept", "type": "unknown", "instructions": "Accept?"}],
            "unknown question type: unknown",
        ),
        ("thresholds", {"yes": 1.01}, "threshold yes must be between zero and one"),
    ],
)
def test_load_refuses_invalid_definitions(definition_home, field, value, message):
    from hooks.classifier.definitions import DefinitionError, load

    raw = sample()
    raw[field] = value
    write_definition(definition_home, raw)
    with pytest.raises(DefinitionError) as error:
        load("sample")
    assert str(error.value) == message


@pytest.mark.parametrize("runtime", [False, True])
def test_load_replaces_package_with_bundle_then_runtime(definition_home, tmp_path, runtime):
    from hooks.classifier.definitions import load

    package = sample()
    write_definition(definition_home, package)
    bundle = tmp_path / "bundle"
    replacement = sample()
    replacement["thresholds"]["yes"] = 0.75
    replacement["questions"][0]["instructions"] = "Bundle question"
    write_definition(bundle / ".claude" / "classifiers", replacement)
    if runtime:
        replacement["thresholds"]["yes"] = 0.9
        replacement["questions"][0]["instructions"] = "Runtime question"
        write_definition(config.AGENTIHOOKS_HOME / "classifiers", replacement)
    with patch.object(profile_chain, "read_state", return_value={"bundle": {"path": str(bundle)}}):
        loaded = load("sample")
    assert loaded.thresholds == {"yes": 0.9 if runtime else 0.75}
    assert loaded.questions[0].question.instructions == ("Runtime question" if runtime else "Bundle question")
    assert loaded.purpose == "sample"


@pytest.mark.parametrize("renamed", [False, True])
def test_code_rule_overrides_preserve_package_question_keys(definition_home, tmp_path, renamed):
    from hooks.classifier.definitions import DefinitionError, load

    raw = sample()
    raw["rule"] = {"type": "code"}
    write_definition(definition_home, raw)
    bundle = tmp_path / "bundle"
    raw["thresholds"]["yes"] = 0.75
    if renamed:
        raw["questions"][0]["name"] = "changed"
    write_definition(bundle / ".claude" / "classifiers", raw)
    with patch.object(profile_chain, "read_state", return_value={"bundle": {"path": str(bundle)}}):
        if renamed:
            with pytest.raises(DefinitionError) as error:
                load("sample")
            assert str(error.value) == "code rule overrides must preserve package question keys"
        else:
            assert load("sample").thresholds == {"yes": 0.75}


@pytest.mark.parametrize("value", ["0", "1", "0.7", "-0.1", "nan", "invalid"])
def test_named_threshold_overrides_are_validated_and_recorded(definition_home, monkeypatch, value):
    from hooks.classifier.definitions import DefinitionError, load

    write_definition(definition_home, sample())
    before = load("sample")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_SAMPLE_YES", value)
    if value in {"0", "1", "0.7"}:
        after = load("sample")
        assert after.thresholds == {"yes": float(value)}
        assert after.digest != before.digest
    else:
        with pytest.raises(DefinitionError) as error:
            load("sample")
        assert str(error.value) == "threshold yes must be between zero and one"
