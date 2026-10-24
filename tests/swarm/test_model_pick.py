from types import SimpleNamespace

import pytest

from hooks.classifier import Answer, DecisionResult
from scripts.swarm import model_pick
from scripts.swarm.model_pick import ModelPick
from tests.swarm.profile_fixture import validated
from tests.swarm.test_runtime import _launched

AUTO = {"model": "auto", "effort": "auto"}
SONNET_XHIGH, OPUS_MEDIUM, OPUS_HIGH, OPUS_XHIGH = (
    ("sonnet", "xhigh"),
    ("opus", "medium"),
    ("opus", "high"),
    ("opus", "xhigh"),
)


def decision(choice="lower", confidence=0.9):
    return DecisionResult({"option": Answer("choice", choice=choice, confidence=confidence)}, "pplx-decider-v1-27b")


def _cells(options):
    return [(option.model, option.effort) for option in options]


CLAUDE_BANDS = [
    ("S", "urgent", [OPUS_MEDIUM, OPUS_HIGH]),
    ("S", "high", [OPUS_MEDIUM, OPUS_HIGH]),
    ("S", "normal", [SONNET_XHIGH, OPUS_MEDIUM]),
    ("S", "low", [SONNET_XHIGH, OPUS_MEDIUM]),
    ("M", "urgent", [OPUS_HIGH, OPUS_XHIGH]),
    ("M", "high", [OPUS_HIGH, OPUS_XHIGH]),
    ("M", "normal", [OPUS_MEDIUM, OPUS_HIGH]),
    ("M", "low", [SONNET_XHIGH, OPUS_MEDIUM]),
    ("L", "urgent", [OPUS_HIGH, OPUS_XHIGH]),
    ("L", "high", [OPUS_HIGH, OPUS_XHIGH]),
    ("L", "normal", [OPUS_HIGH, OPUS_XHIGH]),
    ("L", "low", [OPUS_MEDIUM, OPUS_HIGH]),
    ("", "normal", [OPUS_MEDIUM, OPUS_HIGH]),
    (None, "low", [SONNET_XHIGH, OPUS_MEDIUM]),
    ("M", "", [OPUS_MEDIUM, OPUS_HIGH]),
    ("S", None, [SONNET_XHIGH, OPUS_MEDIUM]),
]


@pytest.mark.parametrize("difficulty,rank,cells", CLAUDE_BANDS)
def test_every_claude_band_cell(difficulty, rank, cells):
    options = model_pick.band("claude", difficulty, rank)
    assert _cells(options) == cells
    assert {option.source for option in options} == {"band"}


def _sol(cells):
    return [("gpt-6.1-sol", effort) for model, effort in cells if model != "sonnet"]


@pytest.mark.parametrize("difficulty,rank,cells", CLAUDE_BANDS)
def test_every_codex_band_cell_is_the_effort_column_on_sol_without_sonnet(difficulty, rank, cells):
    assert _cells(model_pick.band("codex", difficulty, rank)) == _sol(cells)


@pytest.mark.parametrize(
    "harness,difficulty,rank,floor",
    [
        ("claude", "S", "normal", OPUS_MEDIUM),
        ("claude", "M", "low", OPUS_MEDIUM),
        ("claude", "M", "normal", OPUS_MEDIUM),
        ("claude", "L", "normal", OPUS_HIGH),
        ("claude", "S", "urgent", OPUS_MEDIUM),
        ("codex", "S", "normal", ("gpt-6.1-sol", "medium")),
        ("codex", "L", "high", ("gpt-6.1-sol", "high")),
    ],
)
def test_the_band_floor_is_its_first_option_that_is_not_sonnet(harness, difficulty, rank, floor):
    picked = model_pick.floor(model_pick.band(harness, difficulty, rank))
    assert (picked.model, picked.effort) == floor


@pytest.mark.parametrize(
    "difficulty,rank,choice,expected",
    [
        ("S", "normal", "lower", SONNET_XHIGH),
        ("S", "normal", "higher", OPUS_MEDIUM),
        ("M", "normal", "lower", OPUS_MEDIUM),
        ("M", "normal", "higher", OPUS_HIGH),
        ("L", "normal", "lower", OPUS_HIGH),
        ("L", "normal", "higher", OPUS_XHIGH),
        ("M", "low", "lower", SONNET_XHIGH),
        ("M", "urgent", "higher", OPUS_XHIGH),
    ],
)
def test_a_confident_answer_maps_to_its_model_and_effort(monkeypatch, difficulty, rank, choice, expected):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision(choice))
    picked = model_pick.pick("claude", AUTO, {"difficulty": difficulty, "rank": rank}, {})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == (*expected, "pplx-decider-v1-27b", 0.9)


@pytest.mark.parametrize("difficulty", ["S", "M", "L", ""])
@pytest.mark.parametrize("rank", ["urgent", "high", "normal", "low"])
@pytest.mark.parametrize("choice", ["lower", "higher"])
def test_sonnet_is_only_ever_picked_at_xhigh_and_never_for_codex(monkeypatch, difficulty, rank, choice):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision(choice))
    task = {"difficulty": difficulty, "rank": rank}
    claude = model_pick.pick("claude", AUTO, task, {})
    assert claude.model != "sonnet" or claude.effort == "xhigh"
    assert model_pick.pick("codex", AUTO, task, {}).model == "gpt-6.1-sol"


@pytest.mark.parametrize("confidence,expected", [(0.59, OPUS_MEDIUM), (0.6, SONNET_XHIGH)])
def test_an_answer_below_the_confidence_floor_falls_back_to_the_band_floor(monkeypatch, confidence, expected):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision("lower", confidence))
    picked = model_pick.pick("claude", AUTO, {"difficulty": "S"}, {})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == (
        *expected,
        "pplx-decider-v1-27b",
        confidence,
    )


@pytest.mark.parametrize(
    "harness,task,expected",
    [
        ("claude", {"difficulty": "S"}, OPUS_MEDIUM),
        ("claude", {}, OPUS_MEDIUM),
        ("claude", {"difficulty": "L"}, OPUS_HIGH),
        ("codex", {"difficulty": "L", "rank": "urgent"}, ("gpt-6.1-sol", "high")),
    ],
)
def test_no_classifier_launches_the_band_floor(harness, task, expected):
    picked = model_pick.pick(harness, AUTO, task, {})
    assert (picked.model, picked.effort, picked.source, picked.confidence) == (*expected, "band", None)


def test_a_one_option_band_never_asks(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("a one option band asked the classifier"))
    picked = model_pick.pick("codex", AUTO, {"difficulty": "S"}, {})
    assert picked == ModelPick("gpt-6.1-sol", "medium", "band")


def test_the_choice_question_carries_difficulty_rank_and_the_band_options(monkeypatch):
    calls = []

    def decide(state, questions, **kwargs):
        calls.append((state, questions, kwargs))
        return decision("higher")

    monkeypatch.setattr(model_pick, "decide", decide)
    task = {"title": "Fix typo", "description": "One word", "kind": "research", "territory": ["docs", "rules"]}
    model_pick.pick("claude", AUTO, {**task, "difficulty": "S", "rank": "low"}, {})
    state, questions, kwargs = calls[0]
    assert state == {
        "title": "Fix typo",
        "description": "One word",
        "kind": "research",
        "territory_size": 2,
        "difficulty": "S",
        "rank": "low",
    }
    assert kwargs == {"purpose": "model-pick", "harness": "claude"}
    assert set(questions) == {"option"}
    question = questions["option"]
    assert question.type == "choice"
    assert "size is S" in question.instructions and "rank is low" in question.instructions
    assert question.options == {
        "lower": "Sonnet at extra high effort: a small change the task fully specifies, with no design choice left",
        "higher": "Opus at medium effort: the task names what to change and how to check it",
    }


def test_an_unsized_unranked_task_is_asked_as_medium_and_normal(monkeypatch):
    calls = []
    monkeypatch.setattr(model_pick, "decide", lambda state, questions, **kw: calls.append(questions) or decision())
    model_pick.pick("codex", AUTO, {}, {})
    instructions = calls[0]["option"].instructions
    assert "size is M" in instructions and "rank is normal" in instructions
    assert calls[0]["option"].options == {
        "lower": "Sol at medium effort: the task names what to change and how to check it",
        "higher": "Sol at high effort: several seams, or a cause to find inside one component",
    }


@pytest.mark.parametrize(
    "option,expected",
    [
        (
            ("opus", "xhigh"),
            "Opus at extra high effort: an unknown cause across several components, or a redesign of a core concept",
        ),
        (("opus", "high"), "Opus at high effort: several seams, or a cause to find inside one component"),
        (("opus", "medium"), "Opus at medium effort: the task names what to change and how to check it"),
        (
            ("sonnet", "xhigh"),
            "Sonnet at extra high effort: a small change the task fully specifies, with no design choice left",
        ),
        (("gpt-6.1-sol", "high"), "Sol at high effort: several seams, or a cause to find inside one component"),
    ],
)
def test_every_option_is_described_in_plain_words(option, expected):
    assert model_pick.described(ModelPick(*option)) == expected


def test_a_model_without_a_label_is_described_by_its_own_name():
    assert model_pick.described(ModelPick("named", "high")) == (
        "named at high effort: several seams, or a cause to find inside one component"
    )


def test_a_lane_with_no_model_counts_as_auto_and_may_take_the_sonnet_step(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision("lower"))
    picked = model_pick.pick("claude", {"effort": "auto"}, {"difficulty": "S"}, {})
    assert (picked.model, picked.effort) == ("sonnet", "xhigh")


def test_an_explicit_lane_effort_never_calls_the_classifier(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("explicit lane called classifier"))
    picked = model_pick.pick("claude", {"model": "opus", "effort": "high"}, {"difficulty": "S"}, {})
    assert picked == ModelPick("opus", "high")


def test_an_absent_lane_is_left_to_the_launch_default(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("missing lane called classifier"))
    assert model_pick.pick("claude", {}, {}, {}) == ModelPick("", "")


def test_a_fixed_lane_model_keeps_its_model_and_takes_the_picked_effort(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision("higher"))
    picked = model_pick.pick("claude", {"model": "fable", "effort": "auto"}, {"difficulty": "L"}, {})
    assert (picked.model, picked.effort) == ("fable", "xhigh")


def test_a_fixed_lane_model_never_takes_the_sonnet_step(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("a one option band asked the classifier"))
    picked = model_pick.pick("claude", {"model": "opus", "effort": "auto"}, {"difficulty": "S"}, {})
    assert picked == ModelPick("opus", "medium", "band")


@pytest.mark.parametrize("difficulty", ["S", "M", "L"])
def test_a_fixed_sonnet_lane_on_auto_effort_always_runs_at_xhigh(monkeypatch, difficulty):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("a fixed Sonnet lane asked the classifier"))
    picked = model_pick.pick("claude", {"model": "sonnet", "effort": "auto"}, {"difficulty": difficulty}, {})
    assert picked == ModelPick("sonnet", "xhigh", "band")


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_an_environment_model_replaces_the_frontier_model_in_the_band(monkeypatch, harness):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision("higher"))
    environ = {f"AGENTIHOOKS_{harness.upper()}_MODEL": "named"}
    picked = model_pick.pick(harness, AUTO, {"difficulty": "L"}, environ)
    assert (picked.model, picked.effort) == ("named", "xhigh")


@pytest.mark.parametrize("bounds,asked", [(("medium", "high"), False), (("low", "max"), True)])
def test_the_sonnet_step_is_offered_only_when_the_range_reaches_xhigh(monkeypatch, bounds, asked):
    calls = []
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: calls.append(a) or decision("lower"))
    picked = model_pick.pick("claude", AUTO, {"difficulty": "S"}, {}, bounds)
    assert bool(calls) is asked
    assert (picked.model, picked.effort) == (("sonnet", "xhigh") if asked else ("opus", "medium"))


def test_a_configured_confidence_floor_keeps_the_band_floor_below_it(monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision("higher", 0.7))
    task = {"difficulty": "M"}
    picked = model_pick.pick("claude", AUTO, task, {"AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE": "0.8"})
    assert (picked.model, picked.effort, picked.confidence) == ("opus", "medium", 0.7)
    picked = model_pick.pick("claude", AUTO, task, {"AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE": "0.7"})
    assert (picked.model, picked.effort, picked.confidence) == ("opus", "high", 0.7)


def test_a_refused_definition_launches_the_band_floor(monkeypatch, tmp_path):
    from hooks import config
    from hooks.classifier import decision_log

    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("picked without a definition"))
    picked = model_pick.pick("claude", AUTO, {}, {"AGENTIHOOKS_CLASSIFIER_MODEL_PICK_CONFIDENCE": "1.5"})
    assert picked == ModelPick("opus", "medium", "band")
    [entry] = decision_log.read("model-pick")
    assert entry["failures"] == [{"model": "definition", "reason": "threshold confidence must be between zero and one"}]


def test_caller_error_is_not_hidden(monkeypatch):
    from hooks.classifier import ClassifierRequestError

    def broken(*args, **kwargs):
        raise ClassifierRequestError("bad question")

    monkeypatch.setattr(model_pick, "decide", broken)
    with pytest.raises(ClassifierRequestError, match="bad question"):
        model_pick.pick("claude", AUTO, {}, {})


def test_the_master_frontier_stays_on_opus_high():
    assert model_pick.frontier("claude") == ModelPick("opus", "high", "frontier")
    assert model_pick.frontier("codex") == ModelPick("gpt-6.1-sol", "high", "frontier")


TASK = {"id": "t1", "title": "Fix typo", "difficulty": "S", "rank": "normal"}


def test_a_small_normal_task_with_no_answer_launches_opus_at_medium(tmp_path, monkeypatch):
    assert _launched(tmp_path, monkeypatch, "eng", TASK) == ["--model", "opus", "--effort", "medium"]


@pytest.mark.parametrize("lane", ["eng", "ci"])
def test_a_confident_small_pick_launches_sonnet_at_xhigh(tmp_path, monkeypatch, lane):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision("lower"))
    assert _launched(tmp_path, monkeypatch, lane, TASK) == ["--model", "sonnet", "--effort", "xhigh"]


def test_a_codex_pick_launches_sol_with_the_picked_effort(tmp_path, monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision("higher"))
    task = {**TASK, "difficulty": "L"}
    launched = _launched(tmp_path, monkeypatch, "eng", task, harness="codex")
    assert launched == ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"']


def test_masters_keep_opus_high_and_planners_take_the_medium_default(tmp_path, monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("an unclassified lane asked"))
    master = _launched(tmp_path, monkeypatch, "master", {"id": "master", "peer": ""})
    assert master == ["--model", "opus", "--effort", "high"]
    assert _launched(tmp_path, monkeypatch, "plan", TASK) == ["--model", "opus", "--effort", "medium"]


def test_a_range_below_xhigh_launches_the_band_floor_instead_of_sonnet(tmp_path, monkeypatch):
    from scripts.swarm import effort_range

    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("a one option band asked the classifier"))
    monkeypatch.setattr(effort_range, "DEFAULT", ("medium", "high"))
    assert _launched(tmp_path, monkeypatch, "eng", TASK) == ["--model", "opus", "--effort", "medium"]


def test_an_explicit_sonnet_lane_keeps_sonnet_at_its_own_effort(tmp_path, monkeypatch):
    lanes = {"eng": {"agent": "claude", "model": "sonnet", "effort": "high"}}
    assert _launched(tmp_path, monkeypatch, "eng", TASK, lanes) == ["--model", "sonnet", "--effort", "high"]


def test_a_sonnet_lane_on_auto_effort_launches_sonnet_at_xhigh(tmp_path, monkeypatch):
    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: pytest.fail("a fixed Sonnet lane asked the classifier"))
    lanes = {"eng": {"agent": "claude", "model": "sonnet", "effort": "auto"}}
    assert _launched(tmp_path, monkeypatch, "eng", TASK, lanes) == ["--model", "sonnet", "--effort", "xhigh"]


def test_spawn_records_and_stores_classifier_choice(tmp_path, monkeypatch):
    import fakeredis

    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import AgentRecord, RedisStore
    from scripts.swarm.tick import placed_record

    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision("higher"))
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
        lanes={"eng": AUTO},
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


def test_spawn_offers_the_sonnet_step_only_inside_the_configured_range(tmp_path, monkeypatch):
    from scripts.swarm.runtime import HerdrRuntime
    from tests.swarm.test_runtime import _passed

    monkeypatch.setattr(model_pick, "decide", lambda *a, **kw: decision("lower"))
    for key in ("AGENTIHOOKS_CLAUDE_MODEL", "AGENTIHOOKS_CLAUDE_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    seen = []

    def launch(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=launch, choose=lambda *a: ("claude", "quota"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"eng": AUTO},
        autonomy="delegate",
        effort_min="medium",
        effort_max="high",
    )
    runtime.spawn(config, "eng", "engineer@a1b2c3-0001", TASK)
    assert _passed(seen[-1]) == ["--model", "opus", "--effort", "medium"]


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
    monkeypatch.setattr(status.HerdrRuntime, "live_names", lambda self: {"a", "old"})
    cli.cmd_status(saved, SimpleNamespace(slug="sw", json=False))
    output = capsys.readouterr().out
    assert "Model source\tConfidence" in output
    assert "\tjev-1.13\t0.72" in output
    saved.put_agent("sw", AgentRecord("old", "ci", "t"))
    cli.cmd_status(saved, SimpleNamespace(slug="sw", json=False))
    old = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("old\t"))
    assert old.split("\t")[-3:-1] == ["-", "-"]
    agent = next(a for a in status.status_report(saved, "sw", {"tasks": []})["agents"] if a["name"] == "a")
    assert (agent["model_source"], agent["model_confidence"]) == ("jev-1.13", 0.72)


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
