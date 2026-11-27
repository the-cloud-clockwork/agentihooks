import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from scripts.swarm import cli, default_models, model_catalog, model_pick, runtime
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_cli import env, home, run  # noqa: F401

CATALOG = {
    "claude": [{"model": "opus"}, {"model": "sonnet"}],
    "codex": [{"model": "gpt-6.1-sol"}, {"model": "gpt-6-sol"}],
}


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    monkeypatch.setattr(model_catalog, "catalog", lambda *args: CATALOG)
    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", "/repo", 2, 1, state="paused"))
    return saved


def test_model_defaults_and_legacy_config(store):
    config = store.config("sw")
    assert (config.claude_model, config.codex_model) == ("opus", "gpt-6.1-sol")
    store.redis.hdel(store.key("sw", "config"), "claude_model", "codex_model")
    assert store.config("sw") == config


@pytest.mark.parametrize("field", ["claude_model", "codex_model"])
def test_create_refuses_custom_unknown_models(store, field):
    with pytest.raises(SwarmError, match="catalog"):
        store.create(SwarmConfig("new", "/repo", 2, 1, **{field: "unknown"}))
    assert store.redis.hgetall(store.key("new", "config")) == {}


def test_create_accepts_catalog_models(store):
    store.create(SwarmConfig("new", "/repo", 2, 1, claude_model="sonnet", codex_model="gpt-6-sol"))
    assert (store.config("new").claude_model, store.config("new").codex_model) == ("sonnet", "gpt-6-sol")


def test_set_persists_and_reports_both_models(store, capsys):
    cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["claude-model=sonnet", "codex-model=gpt-6-sol"]))
    config = store.config("sw")
    assert (config.claude_model, config.codex_model) == ("sonnet", "gpt-6-sol")
    output = json.loads(capsys.readouterr().out)
    assert (output["claude_model"], output["codex_model"]) == ("sonnet", "gpt-6-sol")


@pytest.mark.parametrize("field", ["claude_model", "codex_model"])
@pytest.mark.parametrize("value", ["unknown", "", None, True, ["opus"]])
def test_store_refuses_unknown_models_without_writing(store, field, value):
    before = store.config("sw")
    with pytest.raises(SwarmError, match="catalog"):
        store.update("sw", **{field: value})
    assert store.config("sw") == before


def test_cli_refuses_a_model_from_the_other_harness(store):
    with pytest.raises(SwarmError, match="catalog"):
        cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["claude-model=gpt-6-sol"]))


def test_empty_catalog_refuses_a_new_model_but_allows_other_changes(store, monkeypatch):
    monkeypatch.setattr(model_catalog, "catalog", lambda *args: {"claude": [], "codex": []})
    assert store.update("sw", max_eng=3).max_eng == 3
    with pytest.raises(SwarmError, match="catalog"):
        store.update("sw", claude_model="sonnet")


@pytest.mark.parametrize("agent,model", [("claude", "sonnet"), ("codex", "gpt-6-sol")])
def test_configured_model_is_used_in_auto_band_and_frontier(agent, model, monkeypatch):
    monkeypatch.setattr(model_pick.runner, "run", Mock(side_effect=model_pick.ClassifierUnavailable("offline")))
    picked = model_pick.pick(agent, {"effort": "auto"}, {"difficulty": "S", "rank": "normal"}, {}, default_model=model)
    assert (picked.model, picked.effort) == (model, "xhigh" if model == "sonnet" else "medium")
    assert model_pick.frontier(agent, default_model=model).model == model


@pytest.mark.parametrize("maximum", ["low", "medium", "high"])
def test_auto_sonnet_refuses_a_range_below_extra_high(maximum):
    picked = model_pick.ModelPick("sonnet", "xhigh", model_pick.BAND)
    with pytest.raises(runtime.SpawnError) as caught:
        runtime._model_args("claude", picked.__dict__, {}, ("low", maximum))
    assert (str(caught.value), caught.value.status) == (
        "unsupported automatic Sonnet: the swarm range must allow xhigh",
        "unsupported",
    )


def test_auto_sonnet_launches_at_extra_high_and_keeps_saved_or_explicit_efforts():
    picked = model_pick.ModelPick("sonnet", "xhigh", model_pick.BAND)
    assert runtime._model_args("claude", picked.__dict__, {}, ("medium", "xhigh")) == [
        "--model",
        "sonnet",
        "--effort",
        "xhigh",
    ]
    for source, preserve in ((model_pick.BAND, True), ("lane-default", False)):
        picked = model_pick.ModelPick("sonnet", "high", source)
        assert runtime._model_args("claude", picked.__dict__, {}, ("medium", "high"), preserve) == [
            "--model",
            "sonnet",
            "--effort",
            "high",
        ]


def test_launch_defaults_keep_explicit_choices(store):
    config = replace(store.config("sw"), claude_model="sonnet", codex_model="gpt-6-sol")
    for lane in ("eng", "ci", "plan", "master"):
        picked = runtime._lane_default(lane, "claude", {"effort": "medium"}, config)
        assert (picked.model, picked.effort) == ("sonnet", "high" if lane == "master" else "medium")
    assert runtime._lane_default("plan", "claude", {"model": "opus"}, config).model == "opus"


@pytest.mark.feature("launch-model-suits-harness")
def test_a_lane_default_never_gives_one_harness_the_other_harness_pinned_model(store: RedisStore) -> None:
    config = replace(store.config("sw"), claude_model="sonnet", codex_model="gpt-6-sol")
    assert runtime._lane_default("plan", "codex", {"model": "opus"}, config).model == "gpt-6-sol"
    assert runtime._lane_default("eng", "claude", {"model": "gpt-6.1-sol"}, config).model == "sonnet"
    assert runtime._lane_default("plan", "codex", {"model": "gpt-6-luna"}, config).model == "gpt-6-luna"
    assert runtime._lane_default("plan", "codex", {"model": "auto"}, config).model == "gpt-6-sol"


@pytest.mark.parametrize("harness, expected", [("claude", "opus"), ("codex", "gpt-6.1-sol")])
def test_missing_config_fields_use_harness_defaults(harness, expected):
    assert default_models.model(None, harness) == expected


def test_default_model_lookup_and_refusal_keep_harnesses_separate(store):
    config = replace(store.config("sw"), claude_model="sonnet", codex_model="gpt-6-sol")
    assert default_models.model(config, "claude") == "sonnet"
    assert default_models.model(config, "codex") == "gpt-6-sol"
    assert default_models.refusal({"claude_model": "sonnet", "codex_model": "gpt-6-sol"}, CATALOG) == ""
    assert default_models.refusal({"max_eng": 3}, {}) == ""
    assert (
        default_models.refusal({"claude_model": "gpt-6-sol"}, CATALOG)
        == "claude_model must be a model listed in the claude catalog"
    )
    assert (
        default_models.refusal({"codex_model": "sonnet"}, CATALOG)
        == "codex_model must be a model listed in the codex catalog"
    )


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_missing_harness_catalog_refuses_its_model(harness):
    field = default_models.FIELDS[harness]
    assert default_models.refusal({field: "missing"}, {}) == f"{field} must be a model listed in the {harness} catalog"


@pytest.mark.parametrize("operation", ("create", "update"))
def test_model_validation_uses_the_store_cache_and_current_context(store, operation):
    with (
        patch.object(model_catalog, "catalog", return_value=CATALOG) as load,
        patch.object(runtime.time, "time", return_value=123456.0),
    ):
        if operation == "create":
            store.create(SwarmConfig("new", "/repo", 2, 1, claude_model="sonnet"))
        else:
            store.update("sw", claude_model="sonnet")
    environ, cache, now = load.call_args.args
    context_matches = environ is os.environ
    cache_matches = cache is store.redis
    assert (context_matches, cache_matches, now) == (True, True, 123456.0)


@pytest.mark.feature("lane-model-listed-in-a-harness-catalog")
def test_set_refuses_a_lane_model_no_harness_catalog_lists_without_writing(store: RedisStore) -> None:
    before = store.config("sw")
    with pytest.raises(SwarmError) as refused:
        cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["eng-effort=high", "ci-model=opus-4"]))
    assert str(refused.value) == "ci-model must be auto or a model listed in a harness catalog, not opus-4"
    assert store.config("sw") == before


@pytest.mark.feature("lane-model-listed-in-a-harness-catalog")
def test_set_accepts_a_lane_model_any_harness_catalog_lists(store: RedisStore) -> None:
    cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["eng-model=sonnet", "ci-model=gpt-6-sol"]))
    lanes = store.config("sw").lanes
    assert (lanes["eng"]["model"], lanes["ci"]["model"]) == ("sonnet", "gpt-6-sol")


@pytest.mark.feature("lane-model-listed-in-a-harness-catalog")
@pytest.mark.parametrize(
    "pairs, lane, expected",
    [(["eng-effort=high"], "eng", "retired"), (["plan-model=auto"], "plan", "auto"), (["master-model="], "master", "")],
)
def test_a_set_that_fixes_no_new_lane_model_reads_no_catalog(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, pairs: list[str], lane: str, expected: str
) -> None:
    store.redis.hset(store.key("sw", "config"), "lanes", json.dumps({"eng": {"model": "retired"}}))
    monkeypatch.setattr(model_catalog, "catalog", Mock(side_effect=AssertionError("the catalog was read")))
    cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=pairs))
    assert store.config("sw").lanes[lane]["model"] == expected


@pytest.mark.feature("lane-model-listed-in-a-harness-catalog")
def test_a_lane_update_without_a_model_reads_no_catalog(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    store.redis.hset(store.key("sw", "config"), "lanes", json.dumps({"eng": {"model": "sonnet"}}))
    monkeypatch.setattr(model_catalog, "catalog", Mock(side_effect=AssertionError("the catalog was read")))
    assert store.update("sw", lanes={"eng": {"agent": "codex"}}).lanes == {"eng": {"agent": "codex"}}


@pytest.mark.feature("lane-model-listed-in-a-harness-catalog")
def test_lane_model_validation_uses_the_store_cache_and_current_context(store: RedisStore) -> None:
    with (
        patch.object(model_catalog, "catalog", return_value=CATALOG) as load,
        patch.object(runtime.time, "time", return_value=123456.0),
    ):
        store.update("sw", lanes={"eng": {"model": "sonnet"}})
    environ, cache, now = load.call_args.args
    assert (environ is os.environ, cache is store.redis, now) == (True, True, 123456.0)


@pytest.mark.feature("template-lane-model-listed-in-a-harness-catalog")
def test_create_refuses_a_lane_model_no_harness_catalog_lists_without_writing(store: RedisStore) -> None:
    with pytest.raises(SwarmError) as refused:
        store.create(SwarmConfig("new", "/repo", 2, 1, lanes={"eng": {"model": "sonnet"}, "ci": {"model": "opus-4"}}))
    assert str(refused.value) == "ci-model must be auto or a model listed in a harness catalog, not opus-4"
    assert store.slugs() == ["sw"] and store.redis.hgetall(store.key("new", "config")) == {}


@pytest.mark.feature("template-lane-model-listed-in-a-harness-catalog")
def test_create_accepts_lane_models_any_harness_catalog_lists(store: RedisStore) -> None:
    store.create(SwarmConfig("new", "/repo", 2, 1, lanes={"eng": {"model": "sonnet"}, "ci": {"model": "gpt-6-sol"}}))
    lanes = store.config("new").lanes
    assert (lanes["eng"]["model"], lanes["ci"]["model"]) == ("sonnet", "gpt-6-sol")


@pytest.mark.feature("template-lane-model-listed-in-a-harness-catalog")
def test_a_create_that_fixes_no_lane_model_reads_no_catalog(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_catalog, "catalog", Mock(side_effect=AssertionError("the catalog was read")))
    lanes = {"eng": {"model": "auto"}, "ci": {"model": ""}, "plan": {"agent": "codex"}}
    store.create(SwarmConfig("new", "/repo", 2, 1, lanes=lanes))
    assert store.config("new").lanes == lanes


@pytest.mark.feature("template-lane-model-listed-in-a-harness-catalog")
def test_create_reads_lane_models_through_the_store_cache_and_current_context(store: RedisStore) -> None:
    with (
        patch.object(model_catalog, "catalog", return_value=CATALOG) as load,
        patch.object(runtime.time, "time", return_value=123456.0),
    ):
        store.create(SwarmConfig("new", "/repo", 2, 1, lanes={"eng": {"model": "sonnet"}}))
    environ, cache, now = load.call_args.args
    assert (environ is os.environ, cache is store.redis, now) == (True, True, 123456.0)


@pytest.mark.feature("template-lane-model-listed-in-a-harness-catalog")
def test_create_refuses_a_template_lane_model_no_harness_catalog_lists(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    home: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, _, _ = env
    monkeypatch.setattr(model_catalog, "catalog", Mock(return_value={"claude": [{"model": "opus"}], "codex": []}))
    (home / "swarm-templates").mkdir(parents=True)
    typo = {"name": "typo", "lanes": {"eng": {"model": "opsu"}, "ci": {"model": "opus"}}}
    (home / "swarm-templates" / "typo.json").write_text(json.dumps(typo))
    assert run("sw", "create", "--repo", "/repo", "--template", "typo") == 1
    assert "eng-model must be auto or a model listed in a harness catalog, not opsu" in capsys.readouterr().err
    assert store.slugs() == []
