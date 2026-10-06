import json

import pytest

from scripts.swarm import templates
from scripts.swarm.store import SwarmConfig, SwarmError


@pytest.fixture
def environ(tmp_path):
    return {"AGENTIHOOKS_HOME": str(tmp_path)}


def test_every_built_in_template_loads_with_both_lanes(environ):
    found = {t.name: source for t, source in templates.available(environ)}
    assert {"default", "codex-ci"} <= set(found) and set(found.values()) == {"built-in"}
    for name in found:
        assert set(templates.load(name, environ).lanes) == {"eng", "ci", "plan", "master"}


def test_a_missing_lane_takes_the_default_cap_and_auto_choices():
    t = templates.parse({"name": "x", "lanes": {"eng": {"cap": 4}}})
    assert (t.lanes["eng"].cap, t.lanes["ci"].cap, t.lanes["ci"].agent) == (4, 1, "auto")


@pytest.mark.parametrize(
    "lanes",
    [
        {"qa": {}},
        {"eng": {"agent": "gemini"}},
        {"eng": {"kind": "chores"}},
        {"eng": {"cap": -1}},
        {"eng": {"cap": "2"}},
        {"eng": {"colour": "red"}},
    ],
)
def test_a_bad_lane_is_refused(lanes):
    with pytest.raises(SwarmError):
        templates.parse({"name": "x", "lanes": lanes})


def test_a_bad_name_is_refused():
    with pytest.raises(SwarmError):
        templates.parse({"name": "../x", "lanes": {}})


def test_an_unknown_template_is_refused(environ):
    with pytest.raises(SwarmError, match="no swarm template nope"):
        templates.load("nope", environ)


def test_a_user_template_wins_over_a_built_in_of_the_same_name(environ, tmp_path):
    link = {"from": "eng", "to": "ci", "kind": "delegates-to"}
    t = templates.parse({"name": "default", "lanes": {"eng": {"cap": 7}}, "links": [link], "autonomy": "manual"})
    path = templates.save(t, environ)
    assert path == tmp_path / "swarm-templates" / "default.json"
    assert templates.load("default", environ) == t
    assert dict((x.name, s) for x, s in templates.available(environ))["default"] == "user"
    assert json.loads(path.read_text())["links"] == [link]


def test_a_config_becomes_a_template_with_its_caps_and_lanes():
    config = SwarmConfig("sw", "/r", 3, 0, compact_limit=200, lanes={"eng": {"agent": "codex", "model": "m"}})
    t = templates.from_config("mine", config)
    assert (t.lanes["eng"].cap, t.lanes["ci"].cap, t.compact_limit) == (3, 0, 200)
    assert (t.lanes["eng"].agent, t.lanes["eng"].model, t.lanes["eng"].effort) == ("codex", "m", "auto")


@pytest.mark.parametrize("name", ["../escape", "/etc/escape", "Upper"])
def test_a_lookup_name_outside_the_template_folders_is_refused(environ, tmp_path, name):
    (tmp_path / "escape.json").write_text(json.dumps({"name": "escape", "lanes": {}}))
    with pytest.raises(SwarmError, match="template name"):
        templates.load(name, environ)


def test_template_links_are_kept_and_checked():
    link = {"from": "eng", "to": "ci-1", "kind": "can-observe"}
    assert templates.parse({"name": "x", "links": [link]}).links == [link]


@pytest.mark.parametrize(
    "link",
    [
        {"from": "eng", "to": "ci", "kind": "owns"},
        {"from": "", "to": "ci", "kind": "delegates-to"},
        {"from": "eng", "to": "ci"},
        {"from": "eng", "to": "ci", "kind": "delegates-to", "why": "x"},
        "eng->ci",
    ],
)
def test_a_bad_link_is_refused(link):
    with pytest.raises(SwarmError):
        templates.parse({"name": "x", "links": [link]})
