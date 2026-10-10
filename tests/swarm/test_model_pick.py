from types import SimpleNamespace

import pytest

from hooks.classifier import Answer, DecisionResult
from scripts.swarm import model_pick
from tests.swarm.profile_fixture import validated


def decision(score=0, confidence=0.9):
    return DecisionResult({"effort": Answer("score", score=score, confidence=confidence)}, "pplx-decider-v1-27b")


pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_auto_lane_asks_only_for_effort_and_keeps_the_lane_model(monkeypatch, harness):
    calls = []

    def decide(state, questions, **kwargs):
        calls.append((state, questions, kwargs))
        return decision()

    monkeypatch.setattr(model_pick, "decide", decide)
    task = {"title": "Fix typo", "description": "One word", "kind": "research", "territory": ["docs", "rules"]}
    picked = model_pick.pick(harness, {"model": "auto", "effort": "auto"}, task, {})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == (
        "auto",
        "high",
        "pplx-decider-v1-27b",
        0.9,
    )
    assert calls[0][0] == {"title": "Fix typo", "description": "One word", "kind": "research", "territory_size": 2}
    assert calls[0][2] == {"purpose": "model-pick", "harness": harness}
    assert set(calls[0][1]) == {"effort"}


@pytest.mark.parametrize("confidence,expected", [(0.59, ("auto", "auto")), (0.6, ("auto", "high"))])
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
    picked = model_pick.pick("codex", {"model": "auto", "effort": "auto"}, {}, {})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == ("auto", "auto", "lane-default", None)


def test_explicit_lane_never_calls_classifier(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("explicit lane called classifier"))
    picked = model_pick.pick("claude", {"model": "opus", "effort": "high"}, {}, {})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == ("opus", "high", "lane-default", None)


@pytest.mark.parametrize(
    "harness,score,expected",
    [
        ("claude", -1, "low"),
        ("claude", 0.49, "low"),
        ("claude", 0.5, "low"),
        ("claude", 0.51, "max"),
        ("claude", 1.6, "max"),
        ("claude", 9, "max"),
        ("codex", 9, "xhigh"),
    ],
)
def test_effort_rounds_and_clamps_without_changing_fixed_model(monkeypatch, harness, score, expected):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision(score=score))
    floor = {f"AGENTIHOOKS_{harness.upper()}_EFFORT": "low"}
    picked = model_pick.pick(harness, {"model": "fixed", "effort": "auto"}, {}, floor)
    assert (picked.model, picked.effort) == ("fixed", expected)


@pytest.mark.parametrize(
    "harness,score,expected",
    [
        ("claude", -1, "high"),
        ("claude", 0.4, "high"),
        ("claude", 0.6, "max"),
        ("claude", 2.6, "max"),
        ("codex", 0, "high"),
        ("codex", 1, "xhigh"),
    ],
)
def test_an_effort_answer_only_raises_the_default_effort(monkeypatch, harness, score, expected):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision(score=score))
    assert model_pick.pick(harness, {"model": "auto", "effort": "auto"}, {}, {}).effort == expected


def test_a_configured_default_effort_is_the_floor(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision(score=2))
    picked = model_pick.pick("claude", {"model": "auto", "effort": "auto"}, {}, {"AGENTIHOOKS_CLAUDE_EFFORT": "max"})
    assert (picked.model, picked.effort) == ("auto", "max")


def test_a_default_effort_the_classifier_cannot_rank_is_kept_without_asking(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("an unranked default asked the classifier"))
    lane = {"model": "auto", "effort": "auto"}
    picked = model_pick.pick("claude", lane, {}, {"AGENTIHOOKS_CLAUDE_EFFORT": "xhigh"})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == ("auto", "auto", "lane-default", None)


def test_spawn_records_and_stores_classifier_choice(tmp_path, monkeypatch):
    import fakeredis

    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import AgentRecord, RedisStore
    from scripts.swarm.tick import placed_record

    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision())
    for key in ("AGENTIHOOKS_CLAUDE_MODEL", "AGENTIHOOKS_CLAUDE_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    seen = []

    def launch(argv, **kwargs):
        seen.extend(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=validated(argv, "status=started\nroute_status=routed\nmodel=opus\neffort=high\n"),
            stderr="",
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
    assert seen[seen.index("--model") + 1] == "opus"
    assert seen[seen.index("--effort") + 1] == "high"
    assert (placed.model_source, placed.model_confidence) == ("pplx-decider-v1-27b", 0.9)
    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.put_agent("sw", placed_record(AgentRecord("engineer", "eng", "t1"), placed))
    record = saved.agents("sw")[0]
    assert (record.model, record.effort, record.model_source, record.model_confidence) == (
        "opus",
        "high",
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
    saved.put_agent("sw", AgentRecord("old", "ci", "t"))
    cli.cmd_status(saved, SimpleNamespace(slug="sw", json=False))
    old = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("old\t"))
    assert old.split("\t")[-2:] == ["-", "-"]
    agent = next(a for a in status.status_report(saved, "sw", {"tasks": []})["agents"] if a["name"] == "a")
    assert (agent["model_source"], agent["model_confidence"]) == ("jev-1.13", 0.72)


def test_only_an_auto_effort_asks_the_classifier(monkeypatch):
    calls = []

    def decide(state, questions, **kwargs):
        calls.append((state, questions))
        return decision(score=3, confidence=0.8)

    monkeypatch.setattr(model_pick, "decide", decide)
    picked = model_pick.pick("claude", {"model": "fixed", "effort": "auto"}, {}, {})
    assert (picked.model, picked.effort) == ("fixed", "max")
    assert set(calls[0][1]) == {"effort"}
    assert calls[0][0] == {"title": "", "description": "", "kind": "code", "territory_size": 0}
    assert calls[0][1]["effort"].levels == [
        "high: the task names what to change and how to check it",
        "max: an unknown cause to find across several components, or a redesign of a core concept",
    ]
    assert calls[0][1]["effort"].instructions.startswith("Which reasoning effort does a coding agent need")
    assert set(model_pick.pick("claude", {"model": "auto", "effort": "high"}, {}, {}).__dict__) == {
        "model",
        "effort",
        "source",
        "confidence",
    }
    assert len(calls) == 1


def test_a_configured_confidence_floor_keeps_the_default_below_it(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision(score=3, confidence=0.7))
    lane = {"model": "auto", "effort": "auto"}
    picked = model_pick.pick("claude", lane, {}, {"AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE": "0.8"})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == (
        "auto",
        "auto",
        "pplx-decider-v1-27b",
        0.7,
    )
    picked = model_pick.pick("claude", lane, {}, {"AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE": "0.7"})
    assert (picked.model, picked.effort, picked.confidence) == ("auto", "max", 0.7)


def test_a_legacy_floor_outside_zero_to_one_is_refused_and_keeps_the_lane_default(monkeypatch, tmp_path):
    from hooks import config

    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("picked with a malformed floor"))
    lane = {"model": "auto", "effort": "auto"}
    for value in ("1.5", "nan"):
        picked = model_pick.pick("claude", lane, {}, {"AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE": value})
        assert picked == model_pick.ModelPick("auto", "auto")


def test_a_refused_definition_keeps_the_lane_default(monkeypatch, tmp_path):
    from hooks import config
    from hooks.classifier import decision_log

    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("picked without a definition"))
    lane = {"model": "auto", "effort": "auto"}
    picked = model_pick.pick("claude", lane, {}, {"AGENTIHOOKS_CLASSIFIER_MODEL_PICK_CONFIDENCE": "1.5"})
    assert picked == model_pick.ModelPick("auto", "auto")
    [entry] = decision_log.read("model-pick")
    assert entry["failures"] == [{"model": "definition", "reason": "threshold confidence must be between zero and one"}]


def test_caller_error_is_not_hidden(monkeypatch):
    from hooks.classifier import ClassifierRequestError

    def broken(*args, **kwargs):
        raise ClassifierRequestError("bad question")

    monkeypatch.setattr(model_pick, "decide", broken)
    with pytest.raises(ClassifierRequestError, match="bad question"):
        model_pick.pick("claude", {"effort": "auto"}, {}, {})


def test_absent_lane_has_no_classifier_metadata(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("missing lane called classifier"))
    picked = model_pick.pick("claude", {}, {}, {})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == ("", "", "lane-default", None)


def test_spawn_timeout_names_and_retires_the_failed_agent(tmp_path):
    import subprocess

    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.tick import SpawnError

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 90)

    runtime = HerdrRuntime(home=tmp_path, run=timeout, choose=lambda *a: ("claude", "quota"))
    retired = []
    runtime._terminate = retired.append
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={},
        autonomy="delegate",
    )
    with pytest.raises(SpawnError, match="init-agent timed out for engineer@a1b2c3-0001"):
        runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t", "title": "x"})
    assert retired == ["engineer@a1b2c3-0001"]
