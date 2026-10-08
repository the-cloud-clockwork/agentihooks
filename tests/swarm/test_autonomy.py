import json

import pytest

from scripts.swarm import prompt, templates
from scripts.swarm.store import AUTONOMY, MASTER, SwarmConfig, SwarmError
from tests.swarm.test_cli import env, home, run  # noqa: F401
from tests.swarm.test_runtime import _spawn_env

pytestmark = pytest.mark.xdist_group("fakeredis")

TASK = {"id": "t1", "title": "x", "phase": "p1"}


def build(autonomy=None, lane="eng", **task):
    extra = {} if autonomy is None else {"autonomy": autonomy}
    return prompt.build("sw", "/repo", lane, f"sw-{lane}-1", {**TASK, **task}, **extra)


def test_the_levels_run_from_manual_to_full_and_default_to_delegate():
    assert AUTONOMY == ("manual", "assist", "delegate", "full")
    assert SwarmConfig("sw", "/r", 1, 1).autonomy == "delegate"


def test_an_old_config_reads_as_delegate(env):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.redis.hdel(store.key("sw", "config"), "autonomy")
    assert store.config("sw").autonomy == "delegate"


def test_set_takes_the_level_as_a_word_and_refuses_anything_else(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert store.config("sw").autonomy == "delegate"
    assert run("sw", "set", "autonomy=assist") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["autonomy"] == "assist"
    assert run("sw", "autonomy=manual") == 0
    assert store.config("sw").autonomy == "manual"
    assert run("sw", "set", "autonomy=yolo") == 1
    assert run("sw", "set", "autonomy=2") == 1
    assert store.config("sw").autonomy == "manual"


def test_the_store_refuses_an_unknown_level(env):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    with pytest.raises(SwarmError):
        store.update("sw", autonomy="yolo")


def test_a_template_sets_the_level_and_save_template_keeps_it(env, home):  # noqa: F811
    store, _, _ = env
    (home / "swarm-templates").mkdir(parents=True)
    (home / "swarm-templates" / "careful.json").write_text(json.dumps({"name": "careful", "autonomy": "assist"}))
    assert run("sw", "create", "--repo", "/repo", "--template", "careful") == 0
    assert store.config("sw").autonomy == "assist"
    run("sw", "set", "autonomy=full")
    assert run("sw", "save-template", "copy") == 0
    assert json.loads((home / "swarm-templates" / "copy.json").read_text())["autonomy"] == "full"


def test_a_template_without_a_level_creates_a_delegate_swarm(env, home):  # noqa: F811
    store, _, _ = env
    assert run("sw", "create", "--repo", "/repo", "--template", "default") == 0
    assert store.config("sw").autonomy == "delegate"


def test_a_template_with_an_unknown_level_is_refused():
    with pytest.raises(SwarmError):
        templates.parse({"name": "bad", "autonomy": "yolo"})


def test_spawn_hands_agents_the_level(tmp_path, monkeypatch):
    spawned = _spawn_env(tmp_path, monkeypatch, compact_limit=0, autonomy="assist")
    assert spawned["AGENTIHOOKS_SWARM_AUTONOMY"] == "assist"


def test_delegate_is_todays_prompt_for_every_lane():
    for lane in ("eng", "ci", MASTER):
        assert build("delegate", lane) == build(lane=lane)
    text = build("delegate")
    assert (
        "Queue on green checks with agentihooks swarm sw merge queue <pr url>, then "
        "agentihooks swarm sw wait --on merge <pr url>."
    ) in text
    assert "After merged, run wt.sh done." in text


def test_manual_opens_a_draft_and_stops_for_the_operator():
    text = build("manual")
    assert "open a draft pull request into dev" in text
    assert "never merge" in text
    assert "agentihooks swarm sw block" in text
    assert "Merge on green checks" not in text and "done --pr" not in text


def test_assist_waits_for_an_operator_approval_line_before_merging():
    text = build("assist")
    assert "open the pull request into dev" in text and "draft" not in text.split("5. ", 1)[1].split("\n", 1)[0]
    assert (
        "Queue with agentihooks swarm sw merge queue <pr url> only after an OPERATOR line on the ledger approves it"
    ) in text
    assert 'agentihooks ledger --slug sw --as sw-eng-1 comment tasks/t1 "<plain words: what the pull request' in text
    assert "agentihooks swarm sw done --pr <pr url>" in text
    assert "Merge on green checks" not in text


def test_full_engineers_merge_on_green_like_delegate():
    assert build("full") == build("delegate")
    assert build("full", "ci") == build("delegate", "ci")


def test_the_level_shapes_only_code_and_ci_tasks():
    for kind in ("ops", "tune", "troubleshoot", "research"):
        assert build("manual", kind=kind) == build("delegate", kind=kind)
    assert "open a draft pull request into dev" in build("manual", "ci", kind="ci")


def test_only_a_full_master_queues_follow_ups_without_asking():
    full = build("full", MASTER)
    assert "without asking the operator" in full
    for level in ("manual", "assist", "delegate"):
        assert "without asking the operator" not in build(level, MASTER)
    assert len({build(level) for level in AUTONOMY}) == 3


@pytest.mark.parametrize(("level", "awaiting"), [("assist", "approval"), ("delegate", ""), ("manual", "")])
def test_a_pull_request_in_an_assist_swarm_waits_for_the_operator_approval(env, level, awaiting):  # noqa: F811
    _, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "set", f"autonomy={level}")
    run("sw", "start")
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", "https://github.com/o/r/pull/3") == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["awaiting"]) == ("pr", awaiting)


@pytest.mark.parametrize("lane", ["eng", "ci", MASTER])
def test_every_prompt_says_to_raise_a_priority_when_waiting_on_the_operator(lane):
    text = build(lane=lane)
    led = f"agentihooks ledger --slug sw --as sw-{lane}-1"
    assert "Whenever you wait on the operator" in text
    assert f'{led} priority add <item> "<' in text
    assert f'{led} followup add "<text>" --needs-operator' in text
