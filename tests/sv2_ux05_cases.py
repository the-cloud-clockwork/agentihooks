import hashlib
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from scripts.swarm import command_runner, commands, lifecycle
from scripts.swarm import context_view as cv
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_server
from scripts.swarm_v2.herdr import visibility
from scripts.swarm_v2.herdr.visibility import View
from tests.swarm_ledger.ledger_page import fulfill_events, is_events, ledger_state, loaded, serve_modules, shell_html
from tests.swarm_ledger.test_swarm_layout import URL, status

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = ("operator-workflow.json", "context-evidence.json")
METRIC = "operator_workflow_acceptance"
CASES = {"a": "T-SV2-UX-05-A", "b": "T-SV2-UX-05-B", "c": "T-SV2-UX-05-C"}
NOT_CURRENT = "the swarm view is not current; controls wait for a fresh read"
EVIDENCE_CLASS = (
    "mocked: a fake Redis swarm store is the authoritative state; the tick, the worker Pod's heartbeat and the hub's "
    "terminal checks are writes to it; real Chromium pages at 1920 by 1080 and at a 390 by 844 touch viewport render "
    "the ledger page from the swarm view the ledger server projects from that store, and their control writes enter "
    "the ledger server's lifecycle control; the hive command runner consumes the queue with its terminal commands "
    "stubbed; no ledger HTTP server, worker Pod, herdr server or cluster runs"
)


def fixture() -> dict:
    data = json.loads((FIXTURES / INPUTS[0]).read_text())
    return {**data, "context": json.loads((FIXTURES / INPUTS[1]).read_text())}


def pod(name: str) -> dict:
    return {"pod_namespace": "workers", "pod_name": name, "pod_uid": f"uid-{name}"}


def project(store: RedisStore, slug: str, now_ms: int) -> dict:
    with patch.object(commands, "time", SimpleNamespace(time_ns=lambda: now_ms * 1_000_000)):
        return json.loads(json.dumps(commands.view(store, slug)))


class Swarm:
    """The authoritative store every viewer projects; the tick and the worker are writes to it."""

    def __init__(self, data: dict):
        import fakeredis

        self.data, self.slug, self.ran = data, data["swarm"], []
        self.store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
        self.store.create(SwarmConfig(self.slug, ".", 3, 0, state="stopped"))
        self.agent, self.heartbeat_ms = None, 0
        self.publish()

    def control(self, body: dict) -> tuple[int, str]:
        if body.get("action") == "start":
            self.store.update(self.slug, state="running")
            return 200, ""
        if body.get("action") != "lifecycle":
            return 400, "not a control this fixture serves"
        with (
            patch.object(ledger_server, "swarm_store", return_value=self.store),
            patch.object(ledger_server, "swarm_status", return_value={}),
            patch.object(ledger_server.repository, "read", return_value=self.stamps()),
        ):
            try:
                ledger_server.lifecycle_control(self.slug, body, True)
            except lifecycle.Refused as refused:
                return refused.status, str(refused)
        return 200, ""

    def stamps(self) -> dict:
        rows = {f"tasks/{task}/state": {"rev": rev} for task, rev in self.data["task_revisions"].items()}
        return {"_meta": {"stamps": rows}}

    def launch(self, now_ms: int) -> None:
        worker = self.data["worker"]
        agent = AgentRecord(
            self.store.next_name(self.slug, "eng"),
            "eng",
            worker["task"],
            seat=f"{worker['seat']}@{self.slug}",
            profile=worker["profile"],
            runtime_backend="kubernetes",
            runtime_target=pod(worker["pod"]),
        )
        self.agent = self.store.start_execution(self.slug, agent)
        context = self.data["context"]
        cv.record(self.store, self.slug, context["task"], context["pack"], context["evidence"])
        self.heartbeat(now_ms - self.data["heartbeat_before_ms"])
        self.terminal(now_ms - self.data["terminal_before_ms"])

    def reconnect_worker(self) -> None:
        previous = self.agent
        successor = replace(
            previous, execution_id="", generation=0, runtime_target=pod(self.data["worker"]["successor_pod"])
        )
        self.agent = self.store.start_execution(self.slug, successor, previous.execution_id)
        self.publish()

    def heartbeat(self, at_ms: int) -> None:
        self.heartbeat_ms = at_ms
        self.publish()

    def terminal(self, checked_ms: int) -> None:
        worker = self.data["worker"]
        view = View(self.agent.name, worker["machine"], worker["pane_id"], visibility.LIVE, checked_ms, checked_ms)
        visibility.publish(self.store, self.slug, [view])

    def publish(self) -> None:
        agents = [self.row()] if self.agent else []
        tasks = {"open": 0, "claimed": len(agents), "pr": 0, "blocked": 0, "done": 0}
        payload = status(agents=agents, tasks=tasks, task_revisions=self.data["task_revisions"])
        commands.publish(self.store, self.slug, payload, {})

    def row(self) -> dict:
        at = self.heartbeat_ms / 1000
        return {
            "name": self.agent.name,
            "lane": "eng",
            "harness": "claude",
            "model": "claude-opus-5-5",
            "effort": "high",
            "task": self.agent.task,
            "started_at": self.data["opened_ms"],
            "status": "working",
            "inbox": [],
            "runtime_backend": "kubernetes",
            "generation": self.agent.generation,
            "profile": self.agent.profile,
            "observation": {
                "state": "working",
                "terminal": "reachable",
                "failure": "none",
                "confidence": "confirmed",
                "observed_at": at,
                "sources": {"heartbeat": {"reading": "ok", "observed_at": at, "value": "working"}},
            },
        }

    def view(self, now_ms: int) -> dict:
        return project(self.store, self.slug, now_ms)

    def expand(self, task: str, ref: str) -> tuple[int, dict]:
        try:
            return 200, {"data": cv.expand(self.store, self.slug, task, ref)}
        except cv.Refused as refused:
            return refused.status, {"error": {"code": refused.reason, "message": str(refused)}}

    def run_commands(self) -> list[str]:
        with patch.object(command_runner, "run", lambda argv, env: self.ran.append(argv) or ""):
            return command_runner.consume(self.store, self.slug)

    def current(self) -> AgentRecord:
        return self.store.execution(self.slug, self.agent.execution_id)

    def snapshot(self) -> dict:
        keys = sorted(key for key in self.store.redis.keys("*") if not key.endswith("lifecycle-conflicts"))
        return {key: self.store.redis.dump(key) for key in keys}


class Viewer:
    """One browser viewer: a fresh context with no cached storage, whose stream projects the store on each connect."""

    def __init__(self, browser, swarm: Swarm, viewport: dict, now_ms: int):
        self.swarm, self.now_ms, self.puts, self.dialogs, self.errors = swarm, now_ms, [], [], []
        self.frozen = self.down = self.accept = False
        self.shown = swarm.view(now_ms)
        size = {key: viewport[key] for key in ("width", "height")}
        device = {key: value for key, value in viewport.items() if key not in size}
        self.context = browser.new_context(viewport=size, **device)
        self.context.add_init_script(f"Date.now = () => {now_ms};")
        html = shell_html()
        self.context.route("**/*", lambda route: self.route(route, html))
        serve_modules(self.context)
        self.tab = self.context.new_page()
        self.tab.on("pageerror", lambda error: self.errors.append(str(error)))
        self.tab.on("dialog", self.answer)
        self.tab.goto(URL + "#swarm")
        loaded(self.tab)
        self.tab.locator("#swarm-agents tr").first.wait_for(timeout=3000)

    def answer(self, dialog) -> None:
        self.dialogs.append(dialog.message)
        dialog.accept() if self.accept else dialog.dismiss()

    def route(self, route, html):
        request = route.request
        if is_events(request.url):
            if self.down:
                return route.fulfill(status=503, body="ledger unavailable")
            if not self.frozen:
                self.shown = self.swarm.view(self.now_ms)
            return fulfill_events(route, ledger_state(self.swarm.data["ledger"]), self.shown)
        if "/swarm/context/" in request.url:
            task, ref = (unquote(part) for part in urlsplit(request.url).path.split("/swarm/context/", 1)[1].split("/"))
            code, body = self.swarm.expand(task, ref)
            return route.fulfill(status=code, json=body)
        if "/swarm/" in request.url:
            if request.method == "PUT":
                body = json.loads(request.post_data)
                self.puts.append(body)
                code, text = self.swarm.control(body)
                if code != 200:
                    return route.fulfill(status=code, body=text, content_type="text/plain")
                self.shown = self.swarm.view(self.now_ms)
            return route.fulfill(json=self.shown)
        if request.url.startswith(URL):
            return route.fulfill(body=html, content_type="text/html")
        return route.abort()

    def settled(self) -> None:
        self.tab.wait_for_function("() => !document.querySelector('#swarm-modes button').disabled")

    def wait(self, script: str, arg=None) -> None:
        self.tab.wait_for_function(script, arg=arg, timeout=15000)

    def row(self, name: str) -> str:
        return self.tab.locator(f'[id="agent-{name}"]').inner_text()

    def wait_row(self, name: str, present: str = "", absent: str = "") -> str:
        self.wait(
            """([id, present, absent]) => {
              const row = document.getElementById(id);
              return !!row && row.innerText.includes(present) && (!absent || !row.innerText.includes(absent));
            }""",
            [f"agent-{name}", present, absent],
        )
        return self.row(name)

    def note(self) -> str:
        return self.tab.locator("#swarm-note").inner_text()

    def controls(self, name: str) -> list[dict]:
        return self.tab.eval_on_selector_all(
            f'[id="agent-{name}"] button[data-lifecycle]',
            """buttons => buttons.map((b) => {
              b.scrollIntoView({block: "center", inline: "nearest"});
              const r = b.getBoundingClientRect();
              return {action: b.dataset.lifecycle, text: b.innerText, aria: b.getAttribute("aria-label"),
                title: b.title, danger: b.classList.contains("danger"), disabled: b.disabled,
                opacity: getComputedStyle(b.closest(".sw-acts")).opacity,
                shown: r.width > 0 && r.height > 0 && r.left >= 0 && r.right <= innerWidth};
            })""",
        )

    def button(self, name: str, label: str):
        return self.tab.locator(f'[id="agent-{name}"]').get_by_role("button", name=f"{label} {name}")

    def evidence(self, task: str, ref: str) -> str:
        item = self.tab.locator(f'[id="context-{task}"] .ctx-item').filter(has_text=ref)
        item.locator("button").click()
        item.locator(".ctx-evidence").wait_for(timeout=5000)
        return item.inner_text()

    def close(self) -> None:
        self.context.close()


def labelled(controls: list[dict], name: str) -> bool:
    by_action = lifecycle.BY_ACTION
    for shown in controls:
        control = by_action[shown["action"]]
        expected = f"{control.scope}: {control.consequence}" if not shown["disabled"] else shown["title"]
        if (shown["text"], shown["aria"], shown["danger"]) != (
            control.label,
            f"{control.label} {name}",
            control.destructive,
        ):
            return False
        if shown["title"] != expected:
            return False
    return True


def _inspect(browser, swarm: Swarm, viewport: dict, now_ms: int, counts: Counter) -> dict:
    viewer = Viewer(browser, swarm, viewport, now_ms)
    counts["viewers_opened"] += 1
    name, task = swarm.agent.name, swarm.agent.task
    row = viewer.wait_row(name, "live", "behind heartbeat")
    evidence = viewer.evidence(task, swarm.data["evidence_ref"])
    counts["evidence_opened"] += 1
    shown = {
        "row": row.split("\n"),
        "context_task": viewer.tab.locator("#swarm-context tr").first.locator("td").first.inner_text(),
        "evidence": evidence.split("\n"),
        "errors": viewer.errors,
    }
    viewer.close()
    counts["viewers_closed"] += 1
    return shown


def _launch_and_attach(browser, swarm: Swarm, counts: Counter) -> dict:
    data, opened = swarm.data, swarm.data["opened_ms"]
    launcher = Viewer(browser, swarm, data["viewports"]["desktop"], opened)
    counts["viewers_opened"] += 1
    launcher.tab.locator('#swarm-ctl [data-swarm="start"]').click()
    launcher.settled()
    swarm.launch(opened)
    name = swarm.agent.name
    launcher.wait_row(name, "kubernetes")
    launcher.button(name, "message").focus()
    launcher.tab.keyboard.press("Tab")
    focused = launcher.tab.evaluate("() => document.activeElement.getAttribute('aria-label')")
    opacity = launcher.tab.evaluate("() => getComputedStyle(document.activeElement.closest('.sw-acts')).opacity")
    launcher.tab.keyboard.press("Enter")
    launcher.settled()
    counts["keyboard_controls"] += 1
    runner = swarm.run_commands()
    shown = {
        "puts": [{k: v for k, v in put.items() if k not in ("op", "incarnation")} for put in launcher.puts],
        "keyboard_focus": focused,
        "keyboard_actions_opacity": opacity,
        "note": launcher.note(),
        "runner": runner,
        "terminal_commands": len(swarm.ran),
        "errors": launcher.errors,
    }
    launcher.close()
    counts["viewers_closed"] += 1
    return shown


def _workflow(browser, data: dict) -> tuple[dict, Counter]:
    counts, swarm = Counter(), Swarm(data)
    launched = _launch_and_attach(browser, swarm, counts)
    closed = len(browser.contexts)
    reopened = data["reopened_ms"]
    swarm.heartbeat(reopened - data["heartbeat_before_ms"])
    swarm.terminal(reopened - data["fresh_terminal_before_ms"])
    views = {
        size: _inspect(browser, swarm, data["viewports"][size], reopened, counts) for size in ("desktop", "mobile")
    }
    current = swarm.current()
    store = {"state": swarm.store.config(swarm.slug).state, "agent": current.state, "generation": current.generation}
    return {"launched": launched, "viewers_left_open": closed, "reopened": views, "store": store}, counts


def _case_a(browser, data: dict) -> tuple[dict, dict, Counter]:
    first, counts = _workflow(browser, data)
    second, _ = _workflow(browser, data)
    views, launched = first["reopened"], first["launched"]
    seen = "seen 0m ago"
    checks = {
        "launched_from_the_ledger": launched["puts"][0] == {"action": "start"} and first["store"]["state"] == "running",
        "keyboard_reaches_and_fires_attach": launched["keyboard_focus"].startswith("attach ")
        and launched["keyboard_actions_opacity"] == "1"
        and launched["puts"][1]["control"] == "attach"
        and launched["note"] == "",
        "every_viewer_was_closed_during_execution": first["viewers_left_open"] == 0,
        "reopened_views_show_the_current_observation": all(
            seen in " ".join(v["row"]) and "kubernetes · gen 1 · standard" in v["row"] for v in views.values()
        ),
        "reopened_views_show_the_task_and_its_evidence": all(
            v["context_task"] == data["context"]["task"] and any("records 120 to 124" in line for line in v["evidence"])
            for v in views.values()
        ),
        "desktop_and_mobile_agree": views["desktop"]["row"] == views["mobile"]["row"],
        "page_matches_the_store": first["store"] == {"state": "running", "agent": "working", "generation": 1},
        "no_page_errors": not launched["errors"] and not any(v["errors"] for v in views.values()),
        "second_fixture_matches_the_first": second == first,
    }
    return checks, first, counts


def _phone_cancel(phone: Viewer, name: str, accept: bool) -> None:
    phone.accept = accept
    phone.button(name, "cancel").tap()


def _case_b(browser, data: dict) -> tuple[dict, dict, Counter]:
    counts, swarm, opened = Counter(), Swarm(data), data["opened_ms"]
    swarm.control({"action": "start"})
    swarm.launch(opened)
    name = swarm.agent.name
    phone = Viewer(browser, swarm, data["viewports"]["mobile"], opened)
    counts["viewers_opened"] += 1
    touch = phone.tab.evaluate("() => matchMedia('(hover: none)').matches || matchMedia('(pointer: coarse)').matches")
    controls = phone.controls(name)
    counts["controls_checked"] += len(controls)
    pause = phone.tab.locator('#swarm-ctl [data-swarm="pause"]').get_attribute("title")
    before = swarm.snapshot()
    _phone_cancel(phone, name, accept=False)
    phone.tab.wait_for_timeout(300)
    dismissed = {"puts": len(phone.puts), "unchanged": swarm.snapshot() == before}
    phone.frozen = True
    swarm.reconnect_worker()
    before = swarm.snapshot()
    _phone_cancel(phone, name, accept=True)
    phone.wait("() => document.querySelector('#swarm-note').textContent.includes('stale')")
    counts["refused_controls"] += 1
    refused_note, after = phone.note(), swarm.snapshot()
    successor = swarm.current()
    phone.frozen = False
    phone.wait_row(name, "gen 2")
    phone.tab.wait_for_timeout(1500)
    fresh = {c["action"]: c for c in phone.controls(name)}["cancel"]
    secrets = [swarm.agent.execution_id, data["worker"]["pod"], data["worker"]["successor_pod"], "uid-"]
    cancel = lifecycle.BY_ACTION["cancel"]
    checks = {
        "touch_layout_without_hover": touch,
        "every_control_visible_without_hover": bool(controls)
        and all(c["shown"] and c["opacity"] == "1" for c in controls),
        "every_control_names_its_agent_and_scope": labelled(controls, name),
        "swarm_wide_control_names_its_scope": pause.startswith("one swarm: "),
        "destructive_tap_asks_with_its_scope": phone.dialogs[:1] == [f"cancel {name}? {cancel.consequence}"],
        "dismissed_tap_sent_nothing": dismissed == {"puts": 0, "unchanged": True},
        "cached_page_refused_as_stale": "stale" in refused_note,
        "protected_state_unchanged": after == before,
        "successor_kept_working": (successor.state, successor.generation) == ("working", 2),
        "refusal_discloses_no_identity": not any(s in refused_note for s in secrets),
        "correction_needs_a_new_request": len(phone.puts) == 1
        and fresh["title"] == f"{cancel.scope}: {cancel.consequence}",
        "no_page_errors": phone.errors == [],
    }
    observed = {
        "touch": touch,
        "controls": controls,
        "pause_title": pause,
        "dialogs": phone.dialogs,
        "refused_note": refused_note,
        "puts": [put["control"] for put in phone.puts],
    }
    phone.close()
    counts["viewers_closed"] += 1
    return checks, observed, counts


def _case_c(browser, data: dict) -> tuple[dict, dict, Counter]:
    counts, swarm = Counter(), Swarm(data)
    opened, reopened, desktop = data["opened_ms"], data["reopened_ms"], data["viewports"]["desktop"]
    swarm.control({"action": "start"})
    swarm.launch(opened)
    name = swarm.agent.name
    first = Viewer(browser, swarm, desktop, opened)
    counts["viewers_opened"] += 1
    live = first.wait_row(name, "live", "behind heartbeat")
    first.close()
    counts["viewers_closed"] += 1
    closed = len(browser.contexts)
    swarm.heartbeat(reopened - data["heartbeat_before_ms"])
    viewer = Viewer(browser, swarm, desktop, reopened)
    counts["viewers_opened"] += 1
    cached = viewer.wait_row(name, "behind heartbeat")
    counts["stale_marks"] += 1
    before = swarm.snapshot()
    viewer.down = True
    viewer.wait("() => document.querySelector('#swarm-note').textContent.includes('Last observed state shown')")
    lost = {"note": viewer.note(), "row": viewer.row(name), "controls": viewer.controls(name)}
    during = swarm.snapshot()
    swarm.terminal(reopened - data["fresh_terminal_before_ms"])
    viewer.down = False
    fresh = viewer.wait_row(name, "live", "behind heartbeat")
    viewer.wait("() => !document.querySelector('#swarm-note').textContent")
    viewer.tab.wait_for_timeout(1500)
    restored = {"row": viewer.row(name), "controls": viewer.controls(name)}
    counts["stale_marks_cleared"] += 1
    reread = project(RedisStore(swarm.store.redis), swarm.slug, reopened)
    checks = {
        "fresh_terminal_shown_live_before_close": "behind heartbeat" not in live and "cached" not in live,
        "every_viewer_was_closed": closed == 0,
        "reconnecting_terminal_shown_stale": all(s in cached for s in ("cached", "stale", "behind heartbeat")),
        "lost_read_keeps_the_cached_view_marked": "Last observed state shown" in lost["note"]
        and "behind heartbeat" in lost["row"],
        "lost_read_grants_no_control": bool(lost["controls"])
        and all(c["disabled"] and c["title"] == NOT_CURRENT for c in lost["controls"]),
        "fresh_observation_clears_the_stale_mark": "behind heartbeat" not in restored["row"]
        and "behind heartbeat" not in fresh,
        "controls_return_after_a_fresh_read": any(not c["disabled"] for c in restored["controls"]),
        "no_command_replayed": viewer.puts == [] and swarm.run_commands() == [],
        "outage_wrote_nothing": during == before,
        "restart_reads_the_same_view": reread["executions"] == swarm.view(reopened)["executions"],
        "no_page_errors": viewer.errors == [] and first.errors == [],
    }
    observed = {
        "before_close": live.split("\n"),
        "reconnecting": cached.split("\n"),
        "lost": {**lost, "row": lost["row"].split("\n")},
        "restored": {**restored, "row": restored["row"].split("\n")},
    }
    viewer.close()
    counts["viewers_closed"] += 1
    return checks, observed, counts


RUNNERS = {"a": _case_a, "b": _case_b, "c": _case_c}


def run_case(case: str, browser) -> dict:
    checks, observed, counts = RUNNERS[case](browser, fixture())
    return {
        "case": CASES[case],
        "package": "SV2-UX-05",
        "state": "passed" if all(checks.values()) else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "checks": checks,
        "observed": json.loads(json.dumps(observed)),
        METRIC: dict(sorted(counts.items())),
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
    }
