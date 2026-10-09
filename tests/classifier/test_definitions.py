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
