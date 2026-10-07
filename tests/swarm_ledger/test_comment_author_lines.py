import sys
from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import show

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import new_ledger  # noqa: E402

AUTHORS = ["operator", "swarm-buildout-eng-3", "swarm-buildout-master-2", "codex"]
THREAD = [{"id": f"c{n}", "by": by, "at": n + 1, "text": f"line {n}"} for n, by in enumerate(AUTHORS)]
SECTIONS = ("phases", "tasks", "questions", "followups")


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no chromium: {exc}")
        yield chromium
        chromium.close()


def page_html():
    doc = new_ledger.build_doc(
        {
            "title": "Author lines",
            "overview": "o",
            "phases": [{"title": "phase"}],
            "tasks": [{"title": "task"}],
            "questions": [{"text": "question"}],
            "followups": [{"text": "follow up"}],
        }
    )
    for key in SECTIONS:
        doc[key][0]["comments"] = THREAD
    doc["chat"] = THREAD
    return new_ledger.render(doc, "author-lines", 8765)


def lines(browser):
    tab = browser.new_page(viewport={"width": 1600, "height": 900})
    try:
        show(tab, page_html())
        return tab.evaluate(
            """(sections) => {
              const probe = document.createElement("div");
              document.body.append(probe);
              const token = (name) => { probe.style.color = `var(${name})`; return getComputedStyle(probe).color; };
              const line = (el) => {
                const s = getComputedStyle(el);
                return { by: el.querySelector(".who").textContent, color: s.borderLeftColor, width: s.borderLeftWidth, pad: s.paddingLeft };
              };
              const threads = {};
              for (const key of sections) threads[key] = [...document.querySelectorAll(`#sec-${key} .entry`)].map(line);
              threads.chat = [...document.querySelectorAll("#chat-log .entry")].map(line);
              return { blue: token("--accent"), red: token("--signal"), threads };
            }""",
            list(SECTIONS),
        )
    finally:
        tab.close()


def test_operator_entries_carry_blue_and_agent_entries_carry_red_in_every_thread(browser):
    seen = lines(browser)
    assert seen["blue"] != seen["red"]
    for name, entries in seen["threads"].items():
        assert [e["by"] for e in entries] == ["You", *AUTHORS[1:]], name
        assert entries[0]["color"] == seen["blue"], (name, entries[0])
        for agent in entries[1:]:
            assert agent["color"] == seen["red"], (name, agent)
        assert len({(e["width"], e["pad"]) for e in entries}) == 1, (name, entries)
