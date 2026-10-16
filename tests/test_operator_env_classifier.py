import json
import os
import urllib.error

import pytest

from hooks.classifier import YesNo, core, decide
from hooks.classifier.errors import BackendFailure
from scripts import operator_env
from scripts.gates import intent
from scripts.swarm import cli as swarm_cli
from tests.classifier.fakes import FakeUrlopen, ok

URL = "AGENTIHOOKS_CLASSIFIER_URL"
KEY = "AGENTIHOOKS_CLASSIFIER_LITELLM_KEY"


def _settings(home, env):
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": env}))


def _shell(home, *lines):
    (home / ".profile").write_text("".join(f"{line}\n" for line in lines))


def _environ(home):
    return {"HOME": str(home), "PATH": os.environ["PATH"]}


@pytest.fixture(autouse=True)
def _unset(monkeypatch):
    held = {name: value for name, value in os.environ.items() if name.startswith(operator_env.CLASSIFIER_PREFIX)}
    for name in held:
        monkeypatch.delenv(name)
    yield
    for name in [name for name in os.environ if name.startswith(operator_env.CLASSIFIER_PREFIX)]:
        del os.environ[name]
    os.environ.update(held)


def test_the_address_comes_from_the_claude_settings_and_the_key_from_the_login_shell(tmp_path):
    _settings(tmp_path, {URL: "http://litellm:4000", "OTHER_SETTING": "x"})
    _shell(tmp_path, "export AGENTIHOOKS_CLASSIFIER_LITELLM_KEY=sk-shell", "export UNRELATED=y")
    environ = _environ(tmp_path)
    assert operator_env.classifier(environ) == [KEY, URL]
    assert environ == {**_environ(tmp_path), URL: "http://litellm:4000", KEY: "sk-shell"}


def test_a_name_the_caller_holds_is_kept(tmp_path):
    _settings(tmp_path, {URL: "http://settings:4000"})
    _shell(tmp_path, "export AGENTIHOOKS_CLASSIFIER_LITELLM_KEY=sk-shell")
    environ = {**_environ(tmp_path), URL: "http://caller:4000"}
    assert operator_env.classifier(environ) == [KEY]
    assert environ[URL] == "http://caller:4000"


def test_an_empty_name_counts_as_missing(tmp_path):
    _settings(tmp_path, {URL: "http://settings:4000", KEY: ""})
    _shell(tmp_path, "export AGENTIHOOKS_CLASSIFIER_LITELLM_KEY=sk-shell")
    environ = {**_environ(tmp_path), URL: "", KEY: ""}
    assert operator_env.classifier(environ) == [KEY, URL]
    assert environ[URL] == "http://settings:4000"
    assert environ[KEY] == "sk-shell"


def test_the_settings_value_wins_over_the_shell(tmp_path):
    _settings(tmp_path, {URL: "http://settings:4000"})
    _shell(
        tmp_path, "export AGENTIHOOKS_CLASSIFIER_URL=http://shell:4000", "export AGENTIHOOKS_CLASSIFIER_LITELLM_KEY=k"
    )
    environ = _environ(tmp_path)
    operator_env.classifier(environ)
    assert environ[URL] == "http://settings:4000"
    assert environ[KEY] == "k"


def test_no_shell_runs_when_the_caller_and_settings_hold_both(tmp_path, monkeypatch):
    _settings(tmp_path, {URL: "http://settings:4000"})
    monkeypatch.setattr(operator_env.subprocess, "run", lambda *a, **k: pytest.fail("shell ran"))
    environ = {**_environ(tmp_path), KEY: "sk-caller"}
    assert operator_env.classifier(environ) == [URL]


@pytest.mark.parametrize(
    "settings", ["not json", "[1, 2]", '{"env": ["x"]}', '{"env": {"AGENTIHOOKS_CLASSIFIER_URL": 4}}']
)
def test_an_unreadable_settings_file_leaves_the_shell_as_the_source(tmp_path, settings):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(settings)
    _shell(tmp_path, "export AGENTIHOOKS_CLASSIFIER_URL=http://shell:4000")
    environ = _environ(tmp_path)
    assert operator_env.classifier(environ) == [URL]
    assert environ[URL] == "http://shell:4000"


def test_nothing_found_adds_nothing(tmp_path):
    environ = _environ(tmp_path)
    assert operator_env.classifier(environ) == []
    assert environ == _environ(tmp_path)


def test_the_tick_holds_the_classifier_address_and_key_before_any_slug(tmp_path, monkeypatch):
    _settings(tmp_path, {URL: "http://litellm:4000"})
    _shell(tmp_path, "export AGENTIHOOKS_CLASSIFIER_LITELLM_KEY=sk-shell")
    monkeypatch.setenv("HOME", str(tmp_path))
    seen = []

    class Store:
        def slugs(self):
            seen.append((os.environ.get(URL), os.environ.get(KEY)))
            return []

    swarm_cli.cmd_tick(Store(), None)
    assert seen == [("http://litellm:4000", "sk-shell")]


def _tick_environment(tmp_path, monkeypatch):
    _settings(tmp_path, {URL: "http://litellm:4000"})
    _shell(tmp_path, "export AGENTIHOOKS_CLASSIFIER_LITELLM_KEY=sk-shell")
    monkeypatch.setenv("HOME", str(tmp_path))

    class Store:
        def slugs(self):
            return []

    swarm_cli.cmd_tick(Store(), None)
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", "liquid-d1")


class _Fallback:
    name = "haiku"

    def __init__(self, order):
        self.order = order

    def decide(self, request):
        self.order.append("fallback")
        raise BackendFailure("haiku: down")


def test_an_intent_judge_tries_the_decisions_endpoint_before_any_fallback(tmp_path, monkeypatch):
    _tick_environment(tmp_path, monkeypatch)
    order = []

    def endpoint(request, timeout=None):
        order.append(request.full_url)
        raise urllib.error.URLError("refused")

    monkeypatch.setattr("hooks.classifier.api.urlopen", endpoint)
    monkeypatch.setattr(core, "cli_backends", lambda harness: [_Fallback(order)])
    assert intent.judge({}) == (intent.UNCHECKED, "the classifier did not answer")
    assert order == ["http://litellm:4000/v1/decisions", "fallback"]


def test_an_endpoint_answer_in_the_tick_environment_skips_the_fallback(tmp_path, monkeypatch):
    _tick_environment(tmp_path, monkeypatch)
    order = []
    fake = FakeUrlopen({"liquid-d1": ok()})
    monkeypatch.setattr("hooks.classifier.api.urlopen", fake)
    monkeypatch.setattr(core, "cli_backends", lambda harness: [_Fallback(order)])
    result = decide("typo", {"trivial": YesNo("One line?", true="t", false="f")}, purpose="intent-check")
    assert result.source == "liquid-d1"
    assert [call["url"] for call in fake.calls] == ["http://litellm:4000/v1/decisions"]
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer sk-shell"
    assert order == []
