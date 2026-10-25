import json
import subprocess

import pytest

from hooks.classifier import Answer, DecisionResult
from hooks.context import profile_chain


@pytest.fixture
def filters_dir(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    directory = bundle / ".claude" / "conditions"
    directory.mkdir(parents=True)
    state = profile_chain.state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"bundle": {"path": str(bundle)}, "targets": {"global": {"claude": {"profile": ""}}}}))
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr("hooks.config.CONDITIONS_ENABLED", True)
    return directory


class StubClassifier:
    def __init__(self, yes=True, error=None):
        self.yes = yes
        self.error = error
        self.calls = []

    def __call__(self, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": questions, **kwargs})
        if self.error:
            raise self.error
        noul = 0.9 if self.yes else 0.1
        return DecisionResult(answers={name: Answer(type="noul", noul=noul) for name in questions}, source="stub")


@pytest.fixture
def stub(monkeypatch):
    def install(**kwargs):
        fake = StubClassifier(**kwargs)
        monkeypatch.setattr("hooks.classifier.decide", fake)
        return fake

    return install


@pytest.fixture
def project_filters(filters_dir, tmp_path, monkeypatch):
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    directory = repo / ".agentihooks" / "conditions"
    directory.mkdir(parents=True)
    monkeypatch.chdir(repo)
    return directory
