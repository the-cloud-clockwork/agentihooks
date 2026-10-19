import json
import re
from io import BytesIO
from unittest.mock import patch

from scripts.swarm import freeze
from scripts.swarm_ledger.api.resources import revision
from scripts.swarm_ledger.repository import hierarchy
from tests.swarm_ledger.ledger_page import MODULES, ledger_state, shell_html, show
from tests.swarm_ledger.test_api_v1 import authority_live as _authority_live
from tests.swarm_ledger.test_api_v1 import live as _live
from tests.swarm_ledger.test_api_v1 import request
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser
from tests.swarm_ledger.test_ledger_authority import SLUG, send, server

authority_live = _authority_live
live = _live


def record(target, verb="freeze"):
    return {"id": f"{verb}-{target}", "verb": verb, "target": target, "by": "operator", "at": 1, "reason": ""}


def task(tid, phase, **fields):
    return {"id": tid, "title": tid, "phase": phase, "lane": "eng", "state": "open", **fields}


DOC = {
    "title": "Marks",
    "plans": [{"id": "p1", "title": "One"}, {"id": "p2", "title": "Two"}],
    "phases": [
        {"id": "ph1", "title": "First", "plan": "plans/p1"},
        {"id": "ph2", "title": "Second", "plan": "plans/p2"},
    ],
    "slices": [{"id": "s1", "phase": "phases/ph1", "anchor": "s1"}],
    "tasks": [
        task("t1", "ph1", slice="slices/s1"),
        task("t2", "ph2"),
        task("t3", "ph1", lane="ci"),
        task("t4", "ph2", rank="urgent"),
        task("t5", "ph2", merged_into="t1"),
    ],
}


def marks(freezes, fix_phase="", watched=False):
    doc = {**DOC, "freezes": freezes}
    found = freeze.marks(doc, hierarchy.project(doc)[0], fix_phase, watched)
    return {key: set(found[key]) for key in ("frozen", "held")}


def test_a_frozen_plan_marks_every_node_under_it_and_the_tasks_folded_into_them():
    assert marks([record("plans/p2")]) == {
        "frozen": {"plans/p2", "phases/ph2", "tasks/t2", "tasks/t4", "tasks/t5"},
        "held": set(),
    }


def test_a_frozen_phase_marks_itself_and_its_slices_but_not_its_plan():
    frozen = marks([record("phases/ph1")])["frozen"]
    assert {"phases/ph1", "slices/s1", "tasks/t1", "tasks/t3"} <= frozen
    assert "plans/p1" not in frozen


def test_a_freeze_on_a_plan_gone_from_the_ledger_still_marks_the_phase_and_tasks_under_it():
    doc = {
        **DOC,
        "phases": [*DOC["phases"], {"id": "ph3", "title": "Orphan", "plan": "plans/gone"}],
        "tasks": [*DOC["tasks"], task("t9", "ph3")],
        "freezes": [record("plans/gone")],
    }
    frozen = set(freeze.marks(doc, hierarchy.project(doc)[0], "", False)["frozen"])
    assert frozen == {"phases/ph3", "tasks/t9"}


def test_a_focus_holds_what_lies_outside_it_but_never_urgent_or_fix_phase_tasks():
    assert marks([record("plans/p1", "focus")])["held"] == {"plans/p2", "phases/ph2", "tasks/t2"}
    assert marks([record("plans/p1", "focus")], fix_phase="ph2")["held"] == {"plans/p2", "phases/ph2"}


def test_a_task_focus_keeps_the_nodes_above_it_unheld():
    assert marks([record("tasks/t1", "focus")])["held"] == {"plans/p2", "phases/ph2", "tasks/t2", "tasks/t3"}


def test_lane_records_mark_only_tasks():
    assert marks([record("lane:ci")])["frozen"] == {"tasks/t3"}
    assert marks([record("lane:ci", "focus")])["held"] == {"tasks/t1", "tasks/t2", "tasks/t5"}


def test_a_frozen_node_is_never_also_held():
    assert marks([record("plans/p2"), record("plans/p1", "focus")])["held"] == set()


def test_a_watched_swarm_focus_holds_every_task_but_urgent_and_fix_phase_ones():
    assert marks([], "ph2", watched=True)["held"] == {"tasks/t1", "tasks/t3"}


def test_a_task_that_is_not_open_is_never_held():
    doc = {**DOC, "tasks": [{**DOC["tasks"][1], "state": "done"}], "freezes": [record("plans/p1", "focus")]}
    assert freeze.marks(doc, hierarchy.project(doc)[0], "", False)["held"] == ["plans/p2", "phases/ph2"]


def test_a_focus_on_an_item_gone_from_the_ledger_holds_everything_but_urgent_work():
    held = marks([record("tasks/gone", "focus")])["held"]
    assert {"plans/p1", "plans/p2", "phases/ph1", "phases/ph2", "tasks/t1", "tasks/t2"} <= held
    assert "tasks/t4" not in held


def test_a_lane_focus_never_holds_plans_or_phases_whatever_its_value():
    held = marks([record("lane:ci:night", "focus")])["held"]
    assert not held & {"plans/p1", "plans/p2", "phases/ph1", "phases/ph2"}
    assert {"tasks/t1", "tasks/t3"} <= held


def test_the_streamed_ledger_view_carries_the_marks_and_the_server_limits():
    from scripts.swarm_ledger import ledger_artifacts, ledger_core, ledger_server

    view = ledger_server.ledger_view(ledger_state({**DOC, "freezes": [record("plans/p2")]}))
    assert set(view["marks"]["frozen"]) == {"plans/p2", "phases/ph2", "tasks/t2", "tasks/t4", "tasks/t5"}
    assert view["marks"]["held"] == []
    cap, compact = [0, ledger_server.MAX_CAP], [ledger_server.MIN_COMPACT, ledger_server.MAX_COMPACT]
    assert view["_meta"]["limits"] == {
        "capacity": {"max_eng": cap, "max_ci": cap, "max_plan": cap, "masters_step": cap, "compact_limit": compact},
        "efforts": list(ledger_server.EFFORTS),
        "sync_cooldown_ms": ledger_core.SYNC_COOLDOWN_MS,
        "bin_days": ledger_artifacts.KEEP_DAYS,
    }


def test_the_streamed_view_never_marks_a_doctor_fix_phase_task_held(monkeypatch):
    from scripts.swarm_ledger import ledger_server

    doc = doctor_server(monkeypatch, peer_freezes=None)
    doc = {**doc, "freezes": [record("plans/p1", "focus")]}
    assert "tasks/t6" not in ledger_server.ledger_view(ledger_state(doc), "doctor")["marks"]["held"]
    assert "tasks/t6" in ledger_server.ledger_view(ledger_state(doc))["marks"]["held"]


def doctor_server(monkeypatch, peer_freezes):
    from types import SimpleNamespace

    from scripts.doctor import loop, priming
    from scripts.swarm_ledger import ledger_server

    doc = {**DOC, "phases": [*DOC["phases"], {"id": loop.FIX_PHASE, "title": "Fix", "plan": "plans/p2"}]}
    doc = {**doc, "tasks": [*DOC["tasks"], task("t6", loop.FIX_PHASE)]}
    configs = {"doctor": SimpleNamespace(template=priming.TEMPLATE)}
    store = SimpleNamespace(
        config=lambda slug: configs.get(slug, SimpleNamespace(template="")), peer=lambda slug: f"{slug}-watched"
    )
    documents = {"doctor": ledger_state(doc)}
    if peer_freezes is not None:
        documents["doctor-watched"] = documents["plain-watched"] = {"freezes": peer_freezes}
    repository = SimpleNamespace(exists=lambda slug: slug in documents, get_document=lambda slug: documents[slug])
    monkeypatch.setattr(ledger_server, "swarm_store", lambda: store)
    monkeypatch.setattr(ledger_server, "repository", repository)
    monkeypatch.setattr(ledger_server, "swarm_status", lambda slug, ledger: None)
    return doc


def test_a_focus_in_the_watched_swarm_holds_the_doctor_tasks_in_every_view_it_streams(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm_ledger import ledger_server

    doc = doctor_server(monkeypatch, peer_freezes=[record("plans/p1"), record("plans/p2", "focus")])
    published = []
    hub = SimpleNamespace(has=lambda slug: True, publish=lambda slug, kind, view: published.append(view))
    monkeypatch.setattr(ledger_server, "HUB", hub)
    ledger_server.publish_ledger("doctor", ledger_state(doc))
    streamed = ledger_server.stream_resources("doctor")["ledger"]
    for view in (streamed, published[0]):
        assert set(view["marks"]["held"]) == {"tasks/t1", "tasks/t2", "tasks/t3", "tasks/t5"}
    assert ledger_server.ledger_view(ledger_state(doc))["marks"]["held"] == []
    assert ledger_server.ledger_view(ledger_state(doc), "plain")["marks"]["held"] == []


def test_a_freeze_alone_in_the_watched_swarm_holds_no_doctor_task(monkeypatch):
    from scripts.swarm_ledger import ledger_server

    doc = doctor_server(monkeypatch, peer_freezes=[record("plans/p1")])
    assert ledger_server.ledger_view(ledger_state(doc), "doctor")["marks"]["held"] == []


def test_an_unreadable_swarm_leaves_the_doctor_view_unheld(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm.store import SwarmError
    from scripts.swarm_ledger import ledger_server

    doc = doctor_server(monkeypatch, peer_freezes=[record("plans/p2", "focus")])

    def unreadable(slug):
        raise SwarmError("no swarm")

    monkeypatch.setattr(ledger_server, "swarm_store", lambda: SimpleNamespace(config=unreadable))
    assert ledger_server.ledger_view(ledger_state(doc), "doctor")["marks"] == {"frozen": [], "held": []}


def test_page_saves_apply_through_version_one(live):
    op = {"op": "add", "id": "v1-page-save", "thread": "chat", "text": "Saved through version one"}
    status, reply = request(live, "PUT", "saves", {"ops": [op]})
    assert (status, reply["applied"]) == (200, ["v1-page-save"])
    assert any(row["id"] == "v1-page-save" for row in request(live, "GET", "chat?limit=100")[1]["data"])


def test_swarm_controls_and_the_model_list_answer_through_version_one(live):
    status_body = {"config": {"slug": SLUG, "state": "paused"}}
    with patch.object(server, "swarm_control", return_value=(status_body, None)) as control:
        assert request(live, "PUT", "swarm/controls", {"action": "pause"}) == (200, status_body)
    control.assert_called_once_with(SLUG, ["pause"], "swarm")
    catalog = {"claude": [{"model": "opus"}], "codex": []}
    with patch.object(server, "swarm_models", return_value=catalog):
        assert request(live, "GET", "swarm/models") == (200, {"data": catalog, "revision": revision(catalog)})


def test_a_crossed_effort_range_is_refused_before_it_is_queued(live, monkeypatch):
    from types import SimpleNamespace

    config = SimpleNamespace(effort_min="low", effort_max="medium", lanes={})
    monkeypatch.setattr(server, "swarm_store", lambda: SimpleNamespace(config=lambda slug: config))
    with patch.object(server, "swarm_control") as control:
        status, error = request(live, "PUT", "swarm/controls", {"action": "set", "effort_min": "high"})
    assert (status, error["error"]["message"]) == (400, "effort-min high is above effort-max medium")
    control.assert_not_called()


def test_a_ceiling_alone_below_the_stored_floor_is_refused(live, monkeypatch):
    from types import SimpleNamespace

    config = SimpleNamespace(effort_min="medium", effort_max="high", lanes={})
    monkeypatch.setattr(server, "swarm_store", lambda: SimpleNamespace(config=lambda slug: config))
    with patch.object(server, "swarm_control") as control:
        status, error = request(live, "PUT", "swarm/controls", {"action": "set", "effort_max": "low"})
    assert (status, error["error"]["message"]) == (400, "effort-min medium is above effort-max low")
    control.assert_not_called()


def test_a_set_without_efforts_is_queued_even_when_the_stored_range_is_crossed(live, monkeypatch):
    from types import SimpleNamespace

    config = SimpleNamespace(effort_min="high", effort_max="low", lanes={})
    monkeypatch.setattr(server, "swarm_store", lambda: SimpleNamespace(config=lambda slug: config))
    status_body = {"config": {"slug": SLUG, "state": "running"}}
    with patch.object(server, "swarm_control", return_value=(status_body, None)) as control:
        assert request(live, "PUT", "swarm/controls", {"action": "set", "max_eng": 2}) == (200, status_body)
    control.assert_called_once()


def test_uploaded_media_is_served_from_version_one(live):
    from PIL import Image

    image = BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    status, data, _ = send(
        live,
        "POST",
        f"/api/v1/ledgers/{SLUG}/uploads/media",
        image.getvalue(),
        **{
            "X-Ledger-Token": live["admin"],
            "Content-Type": "application/octet-stream",
            "Origin": sorted(server.ALLOWED_ORIGINS)[0],
        },
    )
    assert status == 200
    media_id = json.loads(data)["id"]
    status, body, ctype = send(live, "GET", f"/api/v1/ledgers/{SLUG}/files/media/{media_id}")
    assert (status, body, ctype) == (200, image.getvalue(), "image/png")
    status, body, _ = send(live, "GET", f"/api/v1/ledgers/{SLUG}/files/media/missing.png")
    assert (status, json.loads(body)["error"]["code"]) == (404, "request_refused")


def test_the_version_one_layout_takes_the_page_layout_shape(live):
    layout = {
        "capacity-box": {"height": 312.5},
        "columns": {"swarm-agents": [120, 80.4]},
        "stats": ["elapsed", "started"],
    }
    status, data, _ = send(
        live,
        "PUT",
        "/api/v1/layout",
        json.dumps(layout).encode(),
        **{"Origin": sorted(server.ALLOWED_ORIGINS)[0], "Content-Type": "application/json"},
    )
    saved = {"capacity-box": {"height": 312}, "columns": {"swarm-agents": [120, 80]}, "stats": ["elapsed", "started"]}
    assert (status, json.loads(data)["data"]) == (200, saved)


def source(name):
    return (MODULES / f"{name}.js").read_text(encoding="utf-8")


def test_every_page_route_goes_through_version_one():
    routes = source("config") + source("api")
    assert not re.search(r"`/api/\$\{SLUG\}`\)|/api/swarm|/api/media|\"/api/layout\"|/media/\$\{SLUG\}", routes)
    assert "/artifacts/${SLUG}" not in routes
    constants = dict(re.findall(r"^export const (\w+) = (.*);$", source("config"), re.M))
    for call in re.findall(r"fetch\(([^,)]+)", source("api")):
        names = re.findall(r"\b[A-Z][A-Z0-9_]*\b", call)
        versioned = any(re.search(r"API_V1|/api/v1/", constants.get(name, "")) for name in names)
        assert "V1" in call or versioned or call == "artifactUrl(id", call


def test_the_page_copies_no_server_rule_or_limit():
    assert not re.search(r"SYNC_COOLDOWN_MS|ARTIFACT_KEEP_DAYS", source("config"))
    assert not re.search(r"\bancestors\b|rank === \"urgent\"", source("freezes"))
    controls = source("controls")
    assert '"xhigh"' not in controls
    assert not re.search(r"\b50\]|1000\]", controls)


browser = chromium_browser


def rendered(browser, ledger, swarm):
    tab = browser.new_page(viewport={"width": 1920, "height": 1080})
    tab.route("**/api/**", lambda route: route.abort())
    show(tab, shell_html(), ledger=ledger, swarm=swarm)
    return tab


SWARM = {
    "config": {"slug": SLUG, "state": "running", "max_eng": 3, "max_ci": 0, "max_plan": 1, "masters_step": 1},
    "compact_limit": 600,
    "agents": [],
    "tasks": {},
}


def test_the_page_renders_the_marks_the_server_sends_without_judging_freezes(browser):
    ledger = ledger_state({**DOC, "freezes": [], "marks": {"frozen": ["tasks/t2"], "held": ["tasks/t3"]}})
    tab = rendered(browser, ledger, SWARM)
    try:
        tab.click("#tasks-box > summary")
        tab.locator("#item-tasks-t3").wait_for()
        classes = tab.evaluate(
            "() => ['t1', 't2', 't3'].map((id) => document.getElementById(`item-tasks-${id}`).className)"
        )
        assert ["frozen" in c for c in classes] == [False, True, False]
        assert ["held" in c for c in classes] == [False, False, True]
    finally:
        tab.close()


def test_capacity_steppers_stop_at_the_bounds_the_server_publishes(browser):
    capacity = {"max_eng": [0, 3], "max_ci": [0, 2], "max_plan": [0, 2], "masters_step": [0, 2]}
    limits = {
        "capacity": {**capacity, "compact_limit": [100, 900]},
        "efforts": ["low", "high"],
        "sync_cooldown_ms": 0,
        "bin_days": 9,
    }
    swarm = {**SWARM, "config": {**SWARM["config"], "effort_min": "low", "effort_max": "high"}}
    tab = rendered(browser, {**DOC, "_meta": {"rev": 1, "limits": limits}}, swarm)
    try:
        off = tab.evaluate(
            """() => Object.fromEntries(['eng_up', 'eng_down', 'ci_down', 'effort_max_up', 'effort_min_up']
              .map((name) => [name, document.querySelector(`[data-swarm=${name}]`).disabled]))"""
        )
        assert off == {
            "eng_up": True,
            "eng_down": False,
            "ci_down": True,
            "effort_max_up": True,
            "effort_min_up": False,
        }
    finally:
        tab.close()
