import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, runtime, status, store, templates, tick
from tests.swarm.test_tick import FakeLedger

pytestmark = pytest.mark.xdist_group("fakeredis")


from tests.swarm.profile_fixture import validated


def test_template_role_defaults_and_master_settings_round_trip(tmp_path):
    environ = {"AGENTIHOOKS_HOME": str(tmp_path)}
    default = templates.parse({"name": "x"})
    assert {k: v.profile for k, v in default.lanes.items()} == {
        "eng": "engineer",
        "ci": "cicd",
        "plan": "planner",
        "master": "master",
    }
    for built_in, _ in templates.available(environ):
        assert {k: v.profile for k, v in built_in.lanes.items()} == {
            "eng": "engineer",
            "ci": "cicd",
            "plan": "planner",
            "master": "master",
        }
    config = store.SwarmConfig(
        "sw",
        "/r",
        3,
        0,
        lanes={
            "eng": {"profile": "qa"},
            "master": {"profile": "planner", "model": "sonnet", "effort": "low"},
        },
    )
    saved = templates.from_config("custom", config)
    assert saved.lanes["eng"].cap == 3 and saved.lanes["ci"].cap == 0
    assert saved.lanes["master"].cap == 1
    path = templates.save(saved, environ)
    assert templates.load("custom", environ) == saved
    assert json.loads(path.read_text())["lanes"]["master"]["profile"] == "planner"
    assert templates.lane_map(saved)["master"] == {
        "role": "",
        "agent": "auto",
        "model": "sonnet",
        "effort": "low",
        "kind": "auto",
        "profile": "planner",
    }


@pytest.mark.parametrize("profile", [None, 1, "", " ", "../qa", "qa/other"])
def test_template_refuses_invalid_profile(profile):
    with pytest.raises(store.SwarmError, match="profile"):
        templates.parse({"name": "x", "lanes": {"eng": {"profile": profile}}})


def test_template_accepts_custom_profile_name():
    assert (
        templates.parse({"name": "x", "lanes": {"master": {"profile": "my-role"}}}).lanes["master"].profile == "my-role"
    )


@pytest.mark.parametrize(
    "lane,profile", [("eng", "engineer"), ("ci", "cicd"), ("plan", "planner"), ("master", "master"), ("eng", "qa")]
)
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_runtime_forwards_and_records_profile(tmp_path, lane, profile, harness):
    import fakeredis

    calls = []

    def launch(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=validated(
                argv,
                f"status=started\nroute_status=direct\npane_id=w:p1\nagent={harness}\nprofile={profile}\nmodel=chosen\neffort=low\n",
            ),
            stderr="",
        )

    lanes = {lane: {"profile": profile}} if profile == "qa" else {}
    config = store.SwarmConfig("sw", str(tmp_path), 1, 0, code="a1b2c3", lanes=lanes)
    rt = runtime.HerdrRuntime(home=tmp_path, run=launch, choose=lambda *_: (harness, "open"))
    placed = rt.spawn(config, lane, "a", {"id": "t", "title": "Proof"})
    argv = calls[0]
    assert argv[argv.index("--profile") + 1] == profile
    assert (placed.profile, placed.model, placed.effort) == (profile, "chosen", "low")
    record = tick._placed(store.AgentRecord("a", lane, "t"), placed)
    saved = store.RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(config)
    saved.put_agent("sw", record)
    assert saved.agents("sw") == [record]
    assert (saved.agents("sw")[0].profile, saved.agents("sw")[0].model) == (profile, "chosen")
    raw = json.loads(saved.redis.hget(saved.key("sw", "agents"), "a"))
    raw.pop("profile")
    saved.redis.hset(saved.key("sw", "agents"), "a", json.dumps(raw))
    assert saved.agents("sw")[0].profile == ""


@pytest.mark.parametrize("profile,expected", [("qa", "qa"), ("engineer", "engineer")])
def test_resume_preserves_profile_or_defaults_for_old_record(tmp_path, profile, expected):
    from tests.swarm.test_runtime import _resuming

    rt, config, agent, seen = _resuming(tmp_path, "c0ffee", harness="codex")
    rt.resume(config, replace(agent, profile=profile), "Resume")
    argv = seen["runs"][0]
    assert argv[argv.index("--profile") + 1] == expected


def test_status_displays_profile_column_and_record_fields(monkeypatch, capsys):
    import fakeredis

    saved = store.RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(store.SwarmConfig("sw", "/r", 1, 0))
    for name, lane, profile, model in [
        ("m", "master", "master", "sonnet"),
        ("e", "eng", "engineer", "gpt-6-luna"),
        ("old", "ci", "", ""),
    ]:
        saved.put_agent(
            "sw", store.AgentRecord(name, lane, "t", harness="codex", profile=profile, model=model, effort="low")
        )
    ledger = FakeLedger([])
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    cli.cmd_status(saved, SimpleNamespace(slug="sw", json=False))
    rows = [line.split("\t") for line in capsys.readouterr().out.splitlines() if "\t" in line]
    assert rows[0] == [
        "Agent",
        "Lane",
        "Harness",
        "Profile",
        "Model",
        "Account",
        "Pane",
        "Task",
        "State",
        "Conversation",
        "Model source",
        "Confidence",
    ]
    agents = {row[0]: row for row in rows[1:]}
    assert agents["m"][3:5] == ["master", "sonnet low"]
    assert agents["e"][3:5] == ["engineer", "gpt-6-luna low"]
    assert agents["old"][3:5] == ["unknown", "unknown"]
    assert agents["old"][5] == "-"
    saved.put_agent("sw", replace(saved.agents("sw")[0], account="primary"))
    cli.cmd_status(saved, SimpleNamespace(slug="sw", json=False))
    assert "\tprimary\t" in capsys.readouterr().out
    report = status.status_report(saved, "sw", ledger.state("sw"))
    assert {a["name"]: (a["profile"], a["model"]) for a in report["agents"]} == {
        "m": ("master", "sonnet"),
        "e": ("engineer", "gpt-6-luna"),
        "old": ("", ""),
    }


def test_create_set_and_save_preserve_master_profile(monkeypatch, tmp_path, capsys):
    import fakeredis

    saved = store.RedisStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cli, "connect", lambda: saved)
    ledger = FakeLedger([])
    ledger.say = lambda *args, **kwargs: None
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    assert cli.main(["sw", "create", "--repo", "/r"]) == 0
    assert cli.main(["sw", "set", "master-profile=planner", "eng-profile=qa"]) == 0
    assert cli.main(["sw", "save-template", "custom"]) == 0
    found = templates.load("custom", dict(AGENTIHOOKS_HOME=str(tmp_path)))
    assert found.lanes["master"].profile == "planner"
    assert found.lanes["eng"].profile == "qa"


def test_unknown_template_lane_names_the_valid_lanes():
    with pytest.raises(store.SwarmError, match="template x names lanes.*qa.*eng, ci, plan and master"):
        templates.parse({"name": "x", "lanes": {"qa": {}}})
