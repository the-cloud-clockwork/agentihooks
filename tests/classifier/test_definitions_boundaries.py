from unittest.mock import patch

import pytest
import yaml

from hooks import config
from hooks.classifier.definitions import DefinitionError, load
from hooks.classifier.questions import NAME_PATTERN
from hooks.context import profile_chain

from .test_definitions import definition_home as definition_home
from .test_definitions import sample, write_definition


def test_unknown_keys_are_sorted_and_separated(definition_home):
    raw = sample()
    raw.update(zeta=1, alpha=2)
    write_definition(definition_home, raw)
    with pytest.raises(DefinitionError) as error:
        load("sample")
    assert str(error.value) == "unknown definition keys: alpha, zeta"


@pytest.mark.parametrize("name", [None, "", 5])
def test_question_name_errors_name_the_field(definition_home, name):
    raw = sample()
    raw["questions"][0]["name"] = name
    write_definition(definition_home, raw)
    with pytest.raises(DefinitionError) as error:
        load("sample")
    assert str(error.value) == "question name must be nonempty text"


def test_question_validation_keeps_the_public_error(definition_home):
    raw = sample()
    raw["questions"][0]["name"] = "bad/name"
    write_definition(definition_home, raw)
    with pytest.raises(DefinitionError) as error:
        load("sample")
    assert str(error.value) == f"question name 'bad/name' must match {NAME_PATTERN.pattern}"


def test_code_rule_can_omit_thresholds(definition_home):
    raw = sample()
    raw["rule"] = {"type": "code"}
    del raw["thresholds"]
    write_definition(definition_home, raw)
    assert load("sample").thresholds == {}


@pytest.mark.parametrize("overridden", [False, True])
def test_malformed_yaml_names_the_definition(definition_home, tmp_path, overridden):
    definition_home.mkdir(parents=True)
    (definition_home / "sample.yaml").write_text("[")
    with pytest.raises(yaml.YAMLError) as parser_error:
        yaml.safe_load("[")
    bundle = tmp_path / "bundle"
    if overridden:
        write_definition(bundle / ".claude" / "classifiers", sample())
    with patch.object(profile_chain, "read_state", return_value={"bundle": {"path": str(bundle)}}):
        with pytest.raises(DefinitionError) as error:
            load("sample")
    assert str(error.value) == f"cannot read definition sample: {parser_error.value}"


def test_override_cannot_change_decision_log_purpose(definition_home, tmp_path):
    write_definition(definition_home, sample())
    raw = sample()
    raw["purpose"] = "other"
    bundle = tmp_path / "bundle"
    write_definition(bundle / ".claude" / "classifiers", raw)
    with patch.object(profile_chain, "read_state", return_value={"bundle": {"path": str(bundle)}}):
        with pytest.raises(DefinitionError) as error:
            load("sample")
    assert str(error.value) == "overrides must preserve package purpose"


@pytest.mark.parametrize(("base_rule", "override_rule"), [("code", "yes"), ("yes", "code"), ("yes", "yes")])
def test_code_key_protection_applies_when_either_rule_is_code(definition_home, tmp_path, base_rule, override_rule):
    raw = sample()
    raw["rule"] = {"type": base_rule, "threshold": "yes"}
    write_definition(definition_home, raw)
    raw["rule"]["type"] = override_rule
    raw["questions"][0]["name"] = "changed"
    bundle = tmp_path / "bundle"
    write_definition(bundle / ".claude" / "classifiers", raw)
    with patch.object(profile_chain, "read_state", return_value={"bundle": {"path": str(bundle)}}):
        if "code" in (base_rule, override_rule):
            with pytest.raises(DefinitionError) as error:
                load("sample")
            assert str(error.value) == "code rule overrides must preserve package question keys"
        else:
            assert load("sample").questions[0].name == "changed"


def test_hyphens_in_classifier_and_threshold_names_map_to_environment_underscores(definition_home, monkeypatch):
    raw = sample()
    raw["thresholds"] = {"min-confidence": 0.6}
    raw["rule"]["threshold"] = "min-confidence"
    write_definition(definition_home, raw)
    (definition_home / "sample.yaml").rename(definition_home / "sample-pick.yaml")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_SAMPLE_PICK_MIN_CONFIDENCE", "0.75")
    assert load("sample-pick").thresholds == {"min-confidence": 0.75}


@pytest.mark.parametrize(
    ("name", "message"),
    [("../sample", "invalid classifier name: ../sample"), ("", "classifier name must be nonempty text")],
)
def test_registry_name_error_has_the_declared_label(definition_home, name, message):
    with pytest.raises(DefinitionError) as error:
        load(name)
    assert str(error.value) == message


def test_runtime_can_supply_a_new_definition_without_a_package_base(definition_home):
    write_definition(config.AGENTIHOOKS_HOME / "classifiers", sample())
    assert load("sample").purpose == "sample"


def test_runtime_code_override_is_checked_without_a_bundle_definition(definition_home, tmp_path):
    raw = sample()
    raw["rule"] = {"type": "code"}
    write_definition(definition_home, raw)
    raw["questions"][0]["name"] = "changed"
    write_definition(config.AGENTIHOOKS_HOME / "classifiers", raw)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with patch.object(profile_chain, "read_state", return_value={"bundle": {"path": str(bundle)}}):
        with pytest.raises(DefinitionError) as error:
            load("sample")
    assert str(error.value) == "code rule overrides must preserve package question keys"
