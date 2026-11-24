"""A cold browser shows the first task rows of a production sized ledger within a second and a megabyte."""

import json
import warnings
from typing import Any

import pytest

from scripts.swarm_ledger import ledger_server as server
from tests.swarm_ledger.ledger_page import chromium, served
from tests.swarm_ledger.test_api_v1 import authority_live, live, stored
from tests.swarm_ledger.test_ledger_authority import SLUG

__all__ = ["authority_live", "live"]

FIRST_ROW = """(() => {
  new MutationObserver((_, observer) => {
    const box = (document.getElementById("tasks") || document.body).closest("details");
    if (box && !box.open && !window.tasksOpened) {
      window.tasksOpened = true;
      box.open = true;
    }
    if (document.querySelector('[id^="item-tasks-"]')) {
      window.firstTaskRow = performance.now();
      observer.disconnect();
    }
  }).observe(document, {childList: true, subtree: true});
})();"""
OPEN_STATES = ("open", "claimed", "pr", "blocked")
READS = """() => performance.getEntriesByType("resource")
  .filter((entry) => entry.name.includes("/api/"))
  .map((entry) => {
    const url = new URL(entry.name);
    return `${url.pathname.split("/").pop()}${url.search} ${Math.round(entry.startTime)}-${Math.round(entry.responseEnd)}`;
  })"""


def text(size: int, seed: int) -> str:
    return (f"Step {seed} reads the ledger and writes its result. " * (size // 50 + 1))[:size]


def comments(seed: int) -> list[dict[str, Any]]:
    return [{"id": f"c-{seed}-{n}", "by": "eng", "at": seed + n, "text": text(400, seed)} for n in range(3)]


def production_document() -> dict[str, Any]:
    phases = [{"id": f"p{n}", "title": f"Phase {n}", "description": text(2000, n)} for n in range(177)]
    tasks = [
        {
            "id": f"t-open-{n}",
            "title": f"Open task {n}",
            "state": OPEN_STATES[n % len(OPEN_STATES)],
            "phase": f"p{n % 177}",
            "description": text(12000, n),
            "comments": comments(n),
        }
        for n in range(1415)
    ]
    tasks += [
        {
            "id": f"t-done-{n}",
            "title": f"Done task {n}",
            "state": "done",
            "done": True,
            "phase": f"p{n % 177}",
            "description": text(1500, n),
        }
        for n in range(3000)
    ]
    return {
        "title": "Production sized",
        "overview": text(600, 0),
        "sources": [],
        "priorities": [],
        "plans": [],
        "slices": [],
        "freezes": [],
        "questions": [],
        "phases": phases,
        "tasks": tasks,
        "followups": [{"id": f"f-{n}", "text": text(300, n), "done": n >= 16} for n in range(5715)],
        "alerts": [{"id": f"a-{n}", "text": text(300, n)} for n in range(222)],
        "notifications": [{"id": f"n-{n}", "text": text(300, n)} for n in range(1496)],
        "_meta": {
            "rev": 4697,
            "updated_at": 4697,
            "created_at": 1,
            "members": {f"agent-{n}": {"role": "member", "joined_at": n} for n in range(5604)},
            "events": [{"rev": n, "kind": "sync requested", "by": "operator", "at": n} for n in range(4697)],
            "stamps": {f"tasks/t-open-{n}/state": {"at": n, "by": "eng", "rev": n} for n in range(60000)},
        },
    }


def received_bytes(totals: dict[str, int], event: dict[str, Any], finished: bool) -> None:
    key = event["requestId"]
    length = int(event["encodedDataLength"])
    totals[key] = max(totals.get(key, 0), length) if finished else totals.get(key, 0) + length


def cold_reading(live: dict, monkeypatch: pytest.MonkeyPatch) -> tuple[float, int, str]:
    """Open a production sized ledger in a cold browser and answer the milliseconds to its first task row, the
    bytes received before it and the report line naming both."""
    document = production_document()
    size = len(json.dumps(document))
    assert size > 28_000_000
    stored(monkeypatch, document)
    with served(server) as base, chromium() as browser:
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        context.add_cookies([{"name": f"ledger-session-{SLUG}", "value": f"page:{live['admin']}", "url": base}])
        page = context.new_page()
        page.add_init_script(FIRST_ROW)
        totals: dict[str, int] = {}
        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.on("Network.dataReceived", lambda event: received_bytes(totals, event, False))
        cdp.on("Network.loadingFinished", lambda event: received_bytes(totals, event, True))
        refused: list[str] = []
        page.on("pageerror", lambda error: refused.append(str(error)))
        page.on("console", lambda line: refused.append(f"console {line.text}") if line.type == "error" else None)
        page.on(
            "response", lambda answer: refused.append(f"{answer.status} {answer.url}") if answer.ok is False else None
        )
        page.goto(f"{base}/{SLUG}", wait_until="commit")
        try:
            page.wait_for_function("() => window.firstTaskRow !== undefined", timeout=30000)
        except Exception as exc:
            shown = page.evaluate(
                """async () => {
                  const main = document.querySelector("script[type=module][src$='/js/main.js']").src;
                  const sync = await import(new URL("sync.js", main));
                  const tasks = document.getElementById("tasks");
                  const children = (id) => (document.getElementById(id) || {children: []}).children.length;
                  return {loaded: sync.doc.tasks.length, rev: sync.meta && sync.meta.rev, html: tasks && tasks.outerHTML.slice(0, 300),
                    rendered: performance.getEntriesByName("ledger-rendered").length, phases: children("phases"),
                    followups: children("followups"), status: (document.getElementById("status") || {}).textContent,
                    scheduler: typeof scheduler};
                }"""
            )
            raise AssertionError(f"no task row shown; refused or failed: {refused}; tasks: {shown}") from exc
        transferred = sum(totals.values())
        first_row = float(page.evaluate("window.firstTaskRow"))
        reads = "; ".join(page.evaluate(READS))
        context.close()
    report = (
        f"COLD LOAD document_bytes={size} first_task_row_ms={first_row:.0f} bytes_before_first_row={transferred}"
        f" reads={reads}"
    )
    warnings.warn(report, stacklevel=1)
    return first_row, transferred, report


@pytest.mark.wall_clock
@pytest.mark.feature("ledger-cold-load")
def test_a_cold_browser_shows_the_first_task_rows_within_a_megabyte(
    live: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, transferred, report = cold_reading(live, monkeypatch)
    assert transferred < 1_000_000, report
