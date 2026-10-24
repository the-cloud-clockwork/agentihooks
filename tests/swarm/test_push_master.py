from dataclasses import replace

import pytest

from scripts.handoff import transfers
from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.profiles import codex_master
from scripts.swarm import capabilities, prompt
from scripts.swarm.capabilities import Capabilities
from scripts.swarm.store import (
    APP_SERVER_TRANSPORT,
    LEGACY_TRANSPORT,
    MASTER,
    AgentRecord,
    RedisStore,
    SwarmConfig,
    SwarmError,
)

pytestmark = pytest.mark.unit
WAIT = "agentihooks swarm sw wait --inbox"


@pytest.mark.parametrize(
    ("harness", "transport", "expected"),
    [
        (
            "claude",
            "",
            Capabilities(push_inbox=True, native_terminal_attach=False, requires_foreground_inbox_wait=False),
        ),
        (
            "codex",
            "",
            Capabilities(push_inbox=False, native_terminal_attach=False, requires_foreground_inbox_wait=True),
        ),
        (
            "codex",
            LEGACY_TRANSPORT,
            Capabilities(push_inbox=False, native_terminal_attach=False, requires_foreground_inbox_wait=True),
        ),
        (
            "codex",
            APP_SERVER_TRANSPORT,
            Capabilities(push_inbox=True, native_terminal_attach=True, requires_foreground_inbox_wait=False),
        ),
        ("", "", Capabilities(push_inbox=False, native_terminal_attach=False, requires_foreground_inbox_wait=False)),
        (
            "gemini",
            APP_SERVER_TRANSPORT,
            Capabilities(push_inbox=False, native_terminal_attach=False, requires_foreground_inbox_wait=False),
        ),
    ],
)
def test_each_execution_advertises_capabilities_from_its_harness_and_transport(harness, transport, expected):
    assert capabilities.of(harness, transport) == expected
    assert capabilities.of_agent(AgentRecord("a", "eng", "t", harness=harness, transport=transport)) == expected


@pytest.mark.parametrize(
    ("harness", "transport", "typed"),
    [
        ("claude", "", False),
        ("codex", LEGACY_TRANSPORT, True),
        ("codex", "", True),
        ("codex", APP_SERVER_TRANSPORT, False),
    ],
)
def test_the_waker_skips_a_recipient_whose_transport_owns_delivery(harness, transport, typed):
    import fakeredis

    inbox = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    worker = AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1", harness=harness, transport=transport)
    assert wake.typed_wake(inbox, worker) is typed
    assert wake.typed_wake(inbox, replace(worker, lane=MASTER)) is False
    assert wake.typed_wake(inbox, replace(worker, pane_id="")) is False


def test_a_push_master_waiting_line_ends_the_turn_and_never_loops_on_wait_inbox():
    line = codex_master.waiting("sw", push=True)
    assert line == (
        "Read agentihooks msg inbox and handle every open item. When no work remains, end your turn: new inbox items "
        "arrive in this thread on their own, so never run agentihooks swarm sw wait --inbox, even where your profile "
        "instructions describe that loop. "
        "Use swarm wait --on checks, reply or task for a specific dependency."
    )
    assert codex_master.waiting("sw") == codex_master.waiting("sw", push=False)
    assert f"run {WAIT} in a foreground tool call" in codex_master.waiting("sw")


def test_a_push_master_handoff_next_reads_the_inbox_without_the_wait_loop():
    text = "Rearm a Monitor on the ledger, then check the `Monitor` output."
    assert codex_master.next_action(text, "sw", push=True) == (
        "Read agentihooks msg inbox, then check the `Monitor` output."
    )
    assert codex_master.next_action("Monitor the PR checks.", "sw", push=True) == "Monitor the PR checks."
    assert codex_master.next_action(text, "sw") == (
        f"Read agentihooks msg inbox and run {WAIT}, then check the `{WAIT}` output."
    )


def test_a_push_master_handoff_transforms_its_next_section_and_transfer():
    task = {
        "handoff": "## Done\nRe-arm the Monitor\n## Next\nRe-arm the Monitor\n",
        "transfer": {"id": "x", "next": "Re-arm the Monitor"},
    }
    pushed = codex_master.handoff(task, "sw", push=True)
    assert pushed["handoff"] == "## Done\nRe-arm the Monitor\n## Next\nRead agentihooks msg inbox\n"
    assert pushed["transfer"] == {"id": "x", "next": "Read agentihooks msg inbox"}
    assert codex_master.handoff(task, "sw")["transfer"]["next"] == f"Read agentihooks msg inbox and run {WAIT}"


def test_the_shared_codex_persona_keeps_the_legacy_wait_loop():
    assert codex_master.persona("Body\n") == "Body\n\n" + codex_master.waiting("<slug>") + "\n"


def test_a_push_codex_persona_carries_the_push_waiting_line_without_the_loop():
    text = "Re-arm the Monitor on the ledger.\nMonitor its checks.\nStart a `Monitor` on long running work.\n"
    persona = codex_master.persona(text, push=True)
    assert persona == (
        "Read agentihooks msg inbox.\nWatch its checks.\n"
        "Start a `agentihooks swarm <slug> wait --on checks|reply|task` on long running work.\n\n"
        + codex_master.waiting("<slug>", push=True)
        + "\n"
    )
    assert "wait --inbox in a foreground tool call" not in persona
    assert codex_master.persona(text) == codex_master.persona(text, push=False)


def _master(transport, harness="codex"):
    task = {
        "id": MASTER,
        "harness": harness,
        "transport": transport,
        "handoff": "## Next\nRe-arm the Monitor\n",
    }
    return prompt.build_master("sw", "/repo", "sw-master-1", task)


def test_a_push_mode_codex_master_prompt_drops_the_wait_inbox_loop():
    text = _master(APP_SERVER_TRANSPORT)
    assert "wait --inbox in a foreground tool call" not in text
    assert codex_master.waiting("sw", push=True) in text
    assert "When writing a handoff, make its Next action read agentihooks msg inbox." in text.splitlines()
    assert "and use agentihooks swarm sw wait --inbox when no work remains" not in text
    assert "## Next\nRead agentihooks msg inbox\n" in text


@pytest.mark.parametrize("transport", [LEGACY_TRANSPORT, ""])
def test_a_legacy_codex_master_prompt_keeps_the_wait_inbox_loop(transport):
    text = _master(transport)
    assert codex_master.waiting("sw") in text
    assert (
        f"When writing a handoff, make its Next action read agentihooks msg inbox and use {WAIT} when no work remains."
    ) in text
    assert f"## Next\nRead agentihooks msg inbox and run {WAIT}\n" in text


def test_a_claude_master_prompt_carries_no_codex_waiting_line():
    text = _master("", harness="claude")
    assert "wait --inbox" not in text
    assert "## Next\nRe-arm the Monitor\n" in text


@pytest.fixture
def seat():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    old = AgentRecord("sw-master-1", MASTER, MASTER, seat="master@sw", state="finished")
    store.put_agent("sw", old)
    store.seats.occupy(old.seat, old.name, 1)
    return store, old


def test_a_push_master_confirms_the_push_transformed_next(seat):
    store, old = seat
    action = "Rearm a Monitor on the ledger."
    transfer = transfers.record(store, "sw", old, "recycle", "# Handoff v2\n## Next\n" + action + "\n", 2)
    new = replace(old, name="sw-master-2", harness="codex", transport=APP_SERVER_TRANSPORT, state="working")
    store.seats.occupy(new.seat, new.name, 3)
    transfers.attach(store, "sw", new)
    with pytest.raises(SwarmError, match="exactly"):
        transfers.confirm(store, "sw", transfer["id"], new, codex_master.next_action(action, "sw"), 4)
    pushed = codex_master.next_action(action, "sw", push=True)
    assert pushed == "Read agentihooks msg inbox."
    assert transfers.confirm(store, "sw", transfer["id"], new, pushed, 5)["continuity"]["next"] == pushed


@pytest.mark.parametrize(
    ("setting", "harness", "expected"),
    [
        (APP_SERVER_TRANSPORT, "codex", APP_SERVER_TRANSPORT),
        (LEGACY_TRANSPORT, "codex", LEGACY_TRANSPORT),
        (APP_SERVER_TRANSPORT, "claude", ""),
    ],
)
def test_the_spawned_task_names_the_transport_its_launch_chooses(setting, harness, expected):
    from scripts.swarm_v2.runtime import codex

    config = SwarmConfig("sw", "/repo", 1, 0, codex_inbox_transport=setting)
    assert codex.transport(config, harness) == expected
    assert codex.transport(SwarmConfig("sw", "/repo", 1, 0), "codex") == LEGACY_TRANSPORT


def test_a_config_without_a_transport_setting_reads_as_legacy():
    from types import SimpleNamespace

    from scripts.swarm_v2.runtime import codex

    assert codex.transport(SimpleNamespace(), "codex") == LEGACY_TRANSPORT


class Built(Exception):
    pass


@pytest.mark.parametrize(
    ("setting", "harness", "expected"),
    [
        (APP_SERVER_TRANSPORT, "codex", {"harness": "codex", "transport": APP_SERVER_TRANSPORT}),
        (LEGACY_TRANSPORT, "codex", {"harness": "codex", "transport": LEGACY_TRANSPORT}),
        (APP_SERVER_TRANSPORT, "claude", {"harness": "claude"}),
    ],
)
def test_the_spawn_hands_the_prompt_the_transport_its_launch_chooses(tmp_path, monkeypatch, setting, harness, expected):
    from types import SimpleNamespace

    from scripts.swarm.runtime import HerdrRuntime

    seen = {}

    def build(slug, repo, lane, name, task, **kwargs):
        seen.update(task)
        raise Built

    for key in ("MODEL", "EFFORT"):
        for agent in ("CLAUDE", "CODEX"):
            monkeypatch.delenv(f"AGENTIHOOKS_{agent}_{key}", raising=False)
    monkeypatch.setattr(prompt, "build", build)
    runtime = HerdrRuntime(home=tmp_path, run=None, choose=lambda *_: (harness, "open"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"eng": {"agent": "auto", "model": "auto", "effort": "auto"}},
        autonomy="delegate",
        codex_inbox_transport=setting,
    )
    with pytest.raises(Built):
        runtime.spawn(config, "eng", "agent@a1b2c3-0001", {"id": "t1", "title": "x"})
    assert {key: seen[key] for key in ("harness", "transport") if key in seen} == expected
