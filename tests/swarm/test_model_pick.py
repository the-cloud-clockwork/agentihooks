from types import SimpleNamespace

import pytest

from hooks.classifier import Answer, DecisionResult
from scripts.swarm import model_pick


def decision(tier="small", score=0, confidence=0.9):
    return DecisionResult(
        {
            "tier": Answer("choice", choice=tier, confidence=confidence),
            "effort": Answer("score", score=score, confidence=confidence),
        },
        "pplx-decider-v1-27b",
    )


@pytest.mark.parametrize("harness,expected", [("claude", "sonnet"), ("codex", "gpt-6-luna")])
def test_auto_lane_picks_task_model_and_effort(monkeypatch, harness, expected):
    calls = []

    def decide(state, questions, **kwargs):
        calls.append((state, questions, kwargs))
        return decision()

    monkeypatch.setattr(model_pick, "decide", decide)
    task = {"title": "Fix typo", "description": "One word", "kind": "code", "territory": ["docs", "rules"]}
    picked = model_pick.pick(harness, {"model": "auto", "effort": "auto"}, task, {})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == (
        expected,
        "low",
        "pplx-decider-v1-27b",
        0.9,
    )
    assert calls[0][0] == {"title": "Fix typo", "description": "One word", "kind": "code", "territory_size": 2}
    assert calls[0][2] == {"purpose": "model-pick", "harness": harness}
    assert set(calls[0][1]) == {"tier", "effort"}


@pytest.mark.parametrize("confidence,expected", [(0.59, ("auto", "auto")), (0.6, ("sonnet", "low"))])
def test_confidence_floor_preserves_lane_default(monkeypatch, confidence, expected):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision(confidence=confidence))
    picked = model_pick.pick("claude", {"model": "auto", "effort": "auto"}, {}, {})
    assert (picked.model, picked.effort) == expected
    assert (picked.source, picked.confidence) == ("pplx-decider-v1-27b", confidence)


def test_classifier_unavailable_keeps_default(monkeypatch):
    from hooks.classifier import ClassifierUnavailable

    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("offline")

    monkeypatch.setattr(model_pick, "decide", unavailable)
    picked = model_pick.pick("codex", {"model": "auto", "effort": "high"}, {}, {})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == ("auto", "high", "lane-default", None)


def test_explicit_lane_never_calls_classifier(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("explicit lane called classifier"))
    picked = model_pick.pick("claude", {"model": "opus", "effort": "high"}, {}, {})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == ("opus", "high", "lane-default", None)


@pytest.mark.parametrize(
    "harness,score,expected",
    [
        ("claude", -1, "low"),
        ("claude", 0.49, "low"),
        ("claude", 0.51, "medium"),
        ("claude", 1.6, "high"),
        ("claude", 9, "max"),
        ("codex", 9, "xhigh"),
    ],
)
def test_effort_rounds_and_clamps_without_changing_fixed_model(monkeypatch, harness, score, expected):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision(score=score))
    picked = model_pick.pick(harness, {"model": "fixed", "effort": "auto"}, {}, {})
    assert (picked.model, picked.effort) == ("fixed", expected)


@pytest.mark.parametrize("harness,default", [("claude", "opus"), ("codex", "gpt-6.1-sol")])
def test_tier_maps_override_named_defaults(monkeypatch, harness, default):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision(tier="large"))
    env = {f"AGENTIHOOKS_MODEL_TIERS_{harness.upper()}": "small=custom, large=big"}
    picked = model_pick.pick(harness, {"model": "auto", "effort": "high"}, {}, env)
    assert (picked.model, picked.effort) == ("big", "high")
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision(tier="medium"))
    assert model_pick.pick(harness, {"model": "auto"}, {}, env).model == default


def test_spawn_records_and_stores_classifier_choice(tmp_path, monkeypatch):
    import fakeredis

    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import AgentRecord, RedisStore
    from scripts.swarm.tick import _placed

    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision())
    seen = []

    def launch(argv, **kwargs):
        seen.extend(argv)
        return SimpleNamespace(
            returncode=0, stdout="status=started\nroute_status=routed\nmodel=sonnet\neffort=low\n", stderr=""
        )

    runtime = HerdrRuntime(home=tmp_path, run=launch, choose=lambda *a: ("claude", "quota"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"eng": {"model": "auto", "effort": "auto"}},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "Fix typo"})
    assert seen[seen.index("--model") + 1] == "sonnet"
    assert seen[seen.index("--effort") + 1] == "low"
    assert (placed.model_source, placed.model_confidence) == ("pplx-decider-v1-27b", 0.9)
    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.put_agent("sw", _placed(AgentRecord("engineer", "eng", "t1"), placed))
    record = saved.agents("sw")[0]
    assert (record.model, record.effort, record.model_source, record.model_confidence) == (
        "sonnet",
        "low",
        "pplx-decider-v1-27b",
        0.9,
    )


def test_status_shows_model_source_and_confidence(monkeypatch, capsys):
    import fakeredis

    from scripts.swarm import cli, status
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
    from tests.swarm.test_tick import FakeLedger

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", "/r", 1, 0))
    saved.put_agent(
        "sw", AgentRecord("a", "eng", "t", model="sonnet", effort="low", model_source="jev-1.13", model_confidence=0.72)
    )
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    cli.cmd_status(saved, SimpleNamespace(slug="sw", json=False))
    output = capsys.readouterr().out
    assert "Model source\tConfidence" in output
    assert "\tjev-1.13\t0.72" in output
    agent = status.status_report(saved, "sw", {"tasks": []})["agents"][0]
    assert (agent["model_source"], agent["model_confidence"]) == ("jev-1.13", 0.72)


@pytest.mark.parametrize("raw", ["unknown=x", "small", "small=", "small=x,", "=model"])
def test_invalid_tier_map_names_the_configuration_error(raw):
    with pytest.raises(ValueError, match="model tiers"):
        model_pick.tier_models("claude", {"AGENTIHOOKS_MODEL_TIERS_CLAUDE": raw})


def test_tier_maps_keep_defaults_and_do_not_mutate_them():
    assert model_pick.tier_models("claude", {}) == {"small": "sonnet", "medium": "opus", "large": "opus"}
    assert model_pick.tier_models("codex", {"AGENTIHOOKS_MODEL_TIERS_CODEX": "small=custom"}) == {
        "small": "custom",
        "medium": "gpt-6.1-sol",
        "large": "gpt-6.1-sol",
    }
    assert model_pick.tier_models("codex", {})["small"] == "gpt-6-luna"


def test_each_auto_field_asks_only_its_question(monkeypatch):
    calls = []

    def decide(state, questions, **kwargs):
        calls.append((state, questions))
        return decision(score=2, confidence=0.8)

    monkeypatch.setattr(model_pick, "decide", decide)
    picked = model_pick.pick("claude", {"model": "fixed", "effort": "auto"}, {}, {})
    assert (picked.model, picked.effort) == ("fixed", "high")
    assert set(calls[0][1]) == {"effort"}
    assert calls[0][0] == {"title": "", "description": "", "kind": "code", "territory_size": 0}
    assert calls[0][1]["effort"].levels == ["low", "medium", "high", "max"]
    assert set(model_pick.pick("claude", {"model": "auto", "effort": "high"}, {}, {}).__dict__) == {
        "model",
        "effort",
        "source",
        "confidence",
    }
    assert set(calls[1][1]) == {"tier"}
    assert calls[1][1]["tier"].options == {
        "small": "Routine, narrowly scoped task with a clear solution",
        "medium": "Task requiring analysis across several components",
        "large": "Complex architecture or uncertain system design",
    }


def test_confidence_uses_minimum_requested_answer_and_configured_floor(monkeypatch):
    result = DecisionResult(
        {"tier": Answer("choice", choice="small", confidence=0.95), "effort": Answer("score", score=0, confidence=0.7)},
        "haiku",
        calibrated=False,
    )
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: result)
    picked = model_pick.pick(
        "claude", {"model": "auto", "effort": "auto"}, {}, {"AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE": "0.8"}
    )
    assert (picked.model, picked.effort, picked.source, picked.confidence) == ("auto", "auto", "haiku", 0.7)
    picked = model_pick.pick(
        "claude", {"model": "auto", "effort": "high"}, {}, {"AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE": "0.8"}
    )
    assert (picked.model, picked.effort, picked.confidence) == ("sonnet", "high", 0.95)


def test_caller_error_is_not_hidden(monkeypatch):
    from hooks.classifier import ClassifierRequestError

    def broken(*args, **kwargs):
        raise ClassifierRequestError("bad question")

    monkeypatch.setattr(model_pick, "decide", broken)
    with pytest.raises(ClassifierRequestError, match="bad question"):
        model_pick.pick("claude", {"model": "auto"}, {}, {})


def test_absent_lane_has_no_classifier_metadata(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("missing lane called classifier"))
    picked = model_pick.pick("claude", {}, {}, {})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == ("", "", "lane-default", None)
