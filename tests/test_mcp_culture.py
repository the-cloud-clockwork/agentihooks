"""The culture MCP category: swarm culture and seat memory as resources, and the engine fitted priming prompt."""

import asyncio
from types import SimpleNamespace

import pytest

from hooks.context import injection_trace
from hooks.mcp import _session, culture

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("mcp-sdk")]

SLUG = "sw"
SEAT = "eng-2@sw"
BAD = "Push straight to dev because review slows the swarm down"
CULTURE = f"# How this swarm works\n- {BAD}\n- Write in plain words\n"
LEARNED = [
    {"maturity": "note", "text": "Wait on checks because idle ticks raise a finding"},
    {"maturity": "data", "text": "Shard took ninety seconds because of the browser"},
    {"maturity": "canon", "text": "Queue on green because the queue retests against dev"},
]
RECAPS = [
    {"occupant": "engineer@sw-2", "task": "t2", "text": "## Done\n- second"},
    {"occupant": "engineer@sw-1", "task": "t1", "text": "## Done\n- first"},
]


class _Memory:
    def __init__(self, recaps: list[dict], learned: list[dict]) -> None:
        self._recaps, self._learned = recaps, learned

    def recaps(self, address: str) -> list[dict]:
        return self._recaps if address == SEAT else []

    def learned(self, address: str) -> list[dict]:
        return self._learned if address == SEAT else []


def _store(text: str = CULTURE, recaps: list[dict] | None = None, learned: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        culture=SimpleNamespace(get=lambda slug: text if slug == SLUG else None),
        memory=_Memory(RECAPS if recaps is None else recaps, LEARNED if learned is None else learned),
        names=SimpleNamespace(resolve=lambda name: name),
        agents=lambda slug: [SimpleNamespace(name="engineer@sw-3", seat=SEAT)] if slug == SLUG else [],
    )


@pytest.fixture(autouse=True)
def stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_TARGET", "AGENTIHOOKS_GATE_QUARANTINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_session, "_ENV_FALLBACK_ALLOWED", True)


def _correct(source: str, text: str, locator: dict) -> None:
    injection_trace.record("sess-culture", source.split(":", 1)[0], source, text, locator)
    injection_trace.correct("sess-culture", source, "/repos/agentihooks", "dev is protected")


def test_the_culture_resource_withholds_a_corrected_line() -> None:
    _correct(f"culture:{SLUG}#2", f"- {BAD}", {"swarm": SLUG, "line": 2})

    text = culture.culture_text(_store(), SLUG)

    assert BAD not in text
    assert "- Write in plain words" in text
    assert text.startswith("# How this swarm works\n")


def test_the_culture_resource_is_empty_for_a_swarm_without_culture() -> None:
    assert culture.culture_text(_store(), "other") == ""


def test_the_seat_resource_carries_the_latest_recap_and_canon_first_notes() -> None:
    text = culture.seat_text(_store(), SLUG, "eng-2")

    assert text.startswith(f"Seat {SEAT}.\nLatest recap, by engineer@sw-2 on task t2:\n## Done\n- second\n")
    assert "- first" not in text
    canon = text.index("- canon: Queue on green")
    note = text.index("- note: Wait on checks")
    assert canon < note
    assert "Shard took ninety seconds" not in text
    assert "1 data entries are kept on the seat and not shown." in text
    assert "4. " not in text


def test_the_seat_resource_withholds_a_corrected_note() -> None:
    _correct(f"learned:{SEAT}#1", LEARNED[0]["text"], {"seat": SEAT, "note": 1})

    text = culture.seat_text(_store(), SLUG, SEAT)

    assert "Wait on checks" not in text
    assert "- canon: Queue on green" in text


def test_an_empty_seat_says_it_has_no_recap_and_no_notes() -> None:
    text = culture.seat_text(_store(recaps=[], learned=[]), SLUG, "eng-9")

    assert "Latest recap: none, no occupant of this seat left one." in text
    assert "Learned notes: none recorded on this seat yet." in text


def test_a_seat_of_another_swarm_is_refused() -> None:
    with pytest.raises(ValueError, match="not a seat of swarm sw"):
        culture.seat_text(_store(), SLUG, "eng-2@other")


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        ("claude", "by mentioning @agentihooks:swarm://sw/culture and @agentihooks:swarm://sw/seats/eng-2."),
        (
            "codex",
            "with read_mcp_resource on server agentihooks, uri swarm://sw/culture and uri swarm://sw/seats/eng-2.",
        ),
        ("copilot", "from the agentihooks MCP resources swarm://sw/culture and swarm://sw/seats/eng-2."),
    ],
)
def test_the_prompt_closes_with_how_its_engine_rereads_the_resources(engine: str, expected: str) -> None:
    text = culture.priming(_store(), engine, SLUG, "eng-2")

    assert text.startswith(f"Swarm culture of {SLUG}, shared by every seat:\n# How this swarm works\n")
    assert f"Seat {SEAT}." in text
    assert text.endswith(expected)


def test_under_stdio_empty_arguments_fall_back_to_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import gates

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.setattr(gates.Who, "from_env", classmethod(lambda cls: SimpleNamespace(name="engineer@sw-3")))

    assert culture.resolve(_store(), "", "", "") == ("codex", SLUG, SEAT)


def test_under_stdio_the_engine_defaults_to_claude() -> None:
    assert culture.resolve(_store(), "", SLUG, "eng-2")[0] == "claude"


def test_under_a_network_transport_every_argument_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.setattr(_session, "_ENV_FALLBACK_ALLOWED", False)

    with pytest.raises(ValueError, match="swarm_culture needs engine, swarm, seat"):
        culture.resolve(_store(), "", "", "")
    assert culture.resolve(_store(), "Codex", SLUG, "eng-2") == ("codex", SLUG, SEAT)


def test_the_server_serves_the_templates_and_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.types import TextContent

    from hooks.mcp import build_server

    monkeypatch.setattr(culture, "_store", _store)
    _correct(f"culture:{SLUG}#2", f"- {BAD}", {"swarm": SLUG, "line": 2})
    server = build_server(["culture"])

    async def served() -> tuple[list[tuple[str, str, str | None]], list[tuple[str, list[str]]], object, object, object]:
        templates = [(t.name, t.uriTemplate, t.mimeType) for t in await server.list_resource_templates()]
        prompts = [(p.name, [a.name for a in p.arguments or []]) for p in await server.list_prompts()]
        culture_read = list(await server.read_resource("swarm://sw/culture"))
        seat_read = list(await server.read_resource("swarm://sw/seats/eng-2"))
        prompt = await server.get_prompt("swarm_culture", {"engine": "claude", "swarm": SLUG, "seat": "eng-2"})
        return templates, prompts, culture_read[0].content, seat_read[0].content, prompt.messages[0].content

    templates, prompts, culture_body, seat_body, content = asyncio.run(served())

    assert isinstance(culture_body, str)
    assert isinstance(seat_body, str)
    assert isinstance(content, TextContent)
    message = content.text

    assert sorted(templates) == [
        ("swarm_culture", "swarm://{slug}/culture", "text/markdown"),
        ("swarm_seat", "swarm://{slug}/seats/{seat}", "text/markdown"),
    ]
    assert prompts == [("swarm_culture", ["engine", "swarm", "seat"])]
    assert BAD not in culture_body
    assert culture_body == culture.culture_text(_store(), SLUG)
    assert seat_body.startswith(f"Seat {SEAT}.")
    assert message == culture.priming(_store(), "claude", SLUG, "eng-2")


def _session_is(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from scripts import gates

    monkeypatch.setattr(gates.Who, "from_env", classmethod(lambda cls: SimpleNamespace(name=name)))


def test_the_whole_prompt_reads_culture_seat_and_reread_line_in_order() -> None:
    store = _store(text="- One line\n", recaps=[], learned=[])

    assert culture.priming(store, "claude", SLUG, "eng-2") == (
        f"Swarm culture of {SLUG}, shared by every seat:\n- One line\n\n"
        f"Seat {SEAT}.\nLatest recap: none, no occupant of this seat left one.\n"
        "Learned notes: none recorded on this seat yet.\n\n"
        "Reread them after compaction by mentioning @agentihooks:swarm://sw/culture and "
        "@agentihooks:swarm://sw/seats/eng-2."
    )


def test_a_swarm_without_culture_says_so_in_the_prompt() -> None:
    text = culture.priming(_store(text=""), "claude", SLUG, "eng-2")

    assert text.startswith(f"Swarm culture of {SLUG}, shared by every seat:\nnone written for this swarm yet.\n\n")


def test_under_stdio_without_a_swarm_the_seat_stays_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _session_is(monkeypatch, "engineer@sw-3")

    with pytest.raises(ValueError, match=r"swarm_culture needs swarm, seat$"):
        culture.resolve(_store(), "", "", "")


def test_under_stdio_an_unregistered_caller_has_no_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    _session_is(monkeypatch, "engineer@sw-9")

    with pytest.raises(ValueError, match=r"swarm_culture needs seat$"):
        culture.resolve(_store(), "", "", "")


def test_the_prompt_with_no_arguments_primes_the_calling_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.types import TextContent

    from hooks.mcp import build_server

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    _session_is(monkeypatch, "engineer@sw-3")
    monkeypatch.setattr(culture, "_store", _store)
    prompt = asyncio.run(build_server(["culture"]).get_prompt("swarm_culture", {}))
    content = prompt.messages[0].content

    assert isinstance(content, TextContent)
    assert content.text == culture.priming(_store(), "codex", SLUG, "eng-2")


def test_the_store_is_the_swarm_redis_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import store

    sentinel = object()
    monkeypatch.setattr(store, "connect", lambda: sentinel)

    assert culture._store() is sentinel


def test_the_stdio_listing_skips_an_unknown_category(monkeypatch: pytest.MonkeyPatch) -> None:
    from hooks.mcp import _stdio

    monkeypatch.setenv("MCP_CATEGORIES", "culture,unknown")
    monkeypatch.setattr(
        _stdio, "get_catalog", lambda: {"prompts": {"culture": [{"name": "swarm_culture"}], "channels": []}}
    )

    assert _stdio.list_served("prompts") == [{"name": "swarm_culture"}]
