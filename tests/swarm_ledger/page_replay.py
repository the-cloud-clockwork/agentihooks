import argparse
import datetime
import json
import sys
import threading
import time
import urllib.parse
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

INPUTS = json.loads(Path(__file__).with_name("page_replay.json").read_text())
SLUG = "replay"
VIEWPORT = {"width": 1920, "height": 1080}
START = datetime.datetime(2026, 10, 6, 15, 30, tzinfo=datetime.UTC)
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201e22f7c2a0000000049454e44ae426082"
)
DETERMINISTIC = """(() => {
  let n = 0;
  crypto.randomUUID = () => `00000000-0000-4000-8000-${String(++n).padStart(12, "0")}`;
  let seed = 7;
  Math.random = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
  const folds = "plan-ledger:replay:fold";
  if (!localStorage.getItem(folds)) localStorage.setItem(folds, JSON.stringify(Object.fromEntries(
    ["overview-box", "notes-box", "questions-box", "phases-box", "followups-box", "stats-box", "art-fold", "chat-fold", "notif-fold", "alert-fold"].map((id) => [id, true]))));
  window.__inflight = 0;
  const send = window.fetch;
  window.fetch = function (...args) {
    window.__inflight++;
    const answer = send.apply(this, args);
    answer.finally(() => { window.__inflight--; }).catch(() => {});
    return answer;
  };
})();"""
JUMP = """() => {
  const pick = document.getElementById("jump-to");
  pick.value = "sec-followups";
  pick.dispatchEvent(new Event("change", { bubbles: true }));
}"""
CAPTURE = """() => {
  const body = document.body.cloneNode(true);
  for (const s of body.querySelectorAll("script")) s.remove();
  const storage = {};
  for (let i = 0; i < localStorage.length; i++) storage[localStorage.key(i)] = localStorage.getItem(localStorage.key(i));
  const fields = [...document.querySelectorAll("input, textarea, select")].map((el) =>
    [el.id || el.dataset.focus || el.getAttribute("aria-label") || el.tagName, el.value, !!el.checked, !!el.disabled]);
  const boxes = [...document.querySelectorAll("[id]")].filter((el) => el.tagName !== "SCRIPT").map((el) => {
    const r = el.getBoundingClientRect();
    return [el.id, Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
  });
  const active = document.activeElement;
  return { title: document.title, hash: location.hash, html: body.outerHTML, fields, boxes,
    storage: Object.fromEntries(Object.entries(storage).sort()),
    focus: active ? active.id || (active.dataset && active.dataset.focus) || active.tagName : "",
    scroll: document.getElementById("main-content").scrollTop };
}"""


class FakeApi:
    def __init__(self):
        self.requests = []
        self.dialogs = []
        self.rev = INPUTS["doc"]["_meta"]["rev"]

    def state(self):
        doc = json.loads(json.dumps(INPUTS["doc"]))
        doc["_meta"]["rev"] = self.rev
        return doc

    def note(self, request):
        path = urllib.parse.urlsplit(request.url).path
        raw = request.post_data_buffer
        body = None if raw is None else f"{len(raw)} bytes"
        if raw and request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(raw)
        self.requests.append(
            {
                "method": request.method,
                "path": path,
                "type": request.headers.get("content-type", ""),
                "token": "x-ledger-token" in request.headers,
                "body": body,
            }
        )
        return path

    def handle(self, route):
        request = route.request
        path = self.note(request)
        if path == f"/api/{SLUG}":
            if request.method == "PUT":
                self.rev += 1
            return route.fulfill(json={**self.state(), "rejected": []})
        if path == f"/api/swarm/{SLUG}":
            return route.fulfill(json=INPUTS["swarm"])
        if path == "/api/layout":
            return route.fulfill(json={} if request.method == "GET" else {"ok": True})
        if path == f"/api/media/{SLUG}":
            return route.fulfill(json=INPUTS["upload"])
        return route.fulfill(status=404, body="not recorded")

    def media(self, route):
        self.note(route.request)
        return route.fulfill(body=PNG, content_type="image/png")

    def artifact(self, route):
        name = self.note(route.request).rsplit("/", 1)[1]
        kind = "application/json" if name.endswith(".json") else "text/markdown"
        return route.fulfill(body=INPUTS["artifacts"][name], content_type=f"{kind}; charset=utf-8")

    def take(self):
        out = sorted(self.requests, key=lambda r: json.dumps(r, sort_keys=True))
        dialogs = self.dialogs
        self.requests, self.dialogs = [], []
        return out, dialogs


def quiet(page):
    last, same = None, 0
    for _ in range(200):
        time.sleep(0.05)
        now = page.evaluate(f"() => [window.__inflight, JSON.stringify(({CAPTURE})())]")
        same = same + 1 if now[0] == 0 and now == last else 0
        if same == 3:
            return
        last = now
    raise TimeoutError("the page did not settle")


def settle(page, ms=0):
    quiet(page)
    if ms:
        page.clock.run_for(ms)
        quiet(page)
    page.clock.run_for(20)
    quiet(page)


def steps():
    def type_line(page, selector, text):
        page.focus(selector)
        page.keyboard.type(text)
        page.keyboard.press("Enter")

    return [
        ("load", lambda page: None),
        ("poll", lambda page: settle(page, 2000)),
        ("swarm tab", lambda page: page.click("#tab-swarm")),
        ("autonomy full", lambda page: page.click('#swarm-modes button[data-autonomy="full"]')),
        ("raise eng cap", lambda page: page.click('button[data-swarm="eng_up"]')),
        ("apply caps", lambda page: page.click('button[data-swarm="apply"]')),
        ("gate mode", lambda page: page.click('button[data-gate="watch"][data-gate-mode="off"]')),
        ("health verdict", lambda page: page.select_option("select.hl-pick >> nth=0", "established")),
        ("lift gate", lambda page: page.dispatch_event('button[data-lift="talk"]', "click")),
        ("restore choice", lambda page: page.dispatch_event('button[data-restore-choice="resume"]', "click")),
        ("refresh quota", lambda page: page.click("#quota-refresh")),
        ("terminate", lambda page: page.dispatch_event('button[data-terminate="ci@replay-4"]', "click")),
        ("stop now", lambda page: page.click('button[data-swarm="stop_now"]')),
        ("grip keyboard", lambda page: (page.focus('[data-grip="height"] >> nth=0'), page.keyboard.press("ArrowDown"))),
        ("layout saved", lambda page: settle(page, 400)),
        ("message agent", lambda page: page.dispatch_event('button[data-message="engineer@replay-2"]', "click")),
        ("chat send", lambda page: type_line(page, "#chat-input", "please rebase")),
        ("chat close", lambda page: page.click("#chat-close")),
        ("ledger tab", lambda page: page.click("#tab-ledger")),
        ("sections all", lambda page: page.click("#sections-all")),
        ("comments all", lambda page: page.click("#comments-all")),
        ("check phase", lambda page: page.click('[data-focus="check:phases/p2"]')),
        ("approve plan", lambda page: page.click("#item-phases-p2 .phase-review button >> nth=0")),
        ("add comment", lambda page: page.click("#item-phases-p3 button.add")),
        ("type comment", lambda page: type_line(page, '[data-focus="compose:phases/p3/comments"]', "Looks **good**")),
        ("answer", lambda page: page.click("#item-questions-q2 .thread button.add")),
        ("type answer", lambda page: type_line(page, '[data-focus="compose:questions/q2/answers"]', "The master")),
        ("edit note", lambda page: page.click("#item-notes-n1 .entry-actions button >> nth=0")),
        ("save note", lambda page: (page.keyboard.type(" now"), page.keyboard.press("Enter"))),
        ("rank", lambda page: page.select_option('[data-focus="rank:tasks/t2"]', "urgent")),
        ("scope", lambda page: page.click('[data-focus="scope:followups/f1"]')),
        ("approve priority", lambda page: page.click("#item-priorities-pr1 button.approve")),
        ("clear priority", lambda page: page.click("#item-priorities-pr2 button.danger")),
        ("bell", lambda page: page.click("#bell")),
        ("notice jump", lambda page: page.click("#notifs .notif-text >> nth=0")),
        ("alerts", lambda page: page.click("#alert-fab")),
        ("claim alert", lambda page: page.click("#alerts .alert-claim")),
        (
            "close alert",
            lambda page: (
                page.click("#alerts .alert-done >> nth=0"),
                type_line(page, "#alerts input >> nth=0", "Trimmed"),
            ),
        ),
        ("artifacts", lambda page: page.click("#art-fab")),
        ("open markdown", lambda page: page.click("#art-list .art-title >> text=Plan summary")),
        ("close markdown", lambda page: page.keyboard.press("Escape")),
        ("open json", lambda page: page.click("#art-list .art-title >> text=Data dump")),
        ("close json", lambda page: page.click("dialog .image-close")),
        ("restore artifact", lambda page: page.click("#art-trash button")),
        ("close artifacts", lambda page: page.click("#art-close")),
        ("pick swarm", lambda page: (page.click("#chat-to-pick"), page.click('#chat-to-menu [data-to="swarm"]'))),
        (
            "attach",
            lambda page: page.set_input_files(
                "#chat-panel input[type=file]", files=[{"name": "a.png", "mimeType": "image/png", "buffer": PNG}]
            ),
        ),
        ("send with image", lambda page: type_line(page, "#chat-input", "see image")),
        ("open image", lambda page: page.click("#chat-log .thumb >> nth=0")),
        ("close image", lambda page: page.keyboard.press("Escape")),
        ("escape chat", lambda page: page.keyboard.press("Escape")),
        ("outline", lambda page: page.dispatch_event("#outline-toggle", "click")),
        ("outline pick", lambda page: page.click('#outline a[data-target="item-tasks-t5"]')),
        ("outline all", lambda page: page.click("#outline-all")),
        ("jump to", lambda page: page.evaluate(JUMP)),
        (
            "title",
            lambda page: (
                page.click("#title-edit"),
                page.fill("#title-input", "Renamed ledger"),
                page.keyboard.press("Enter"),
            ),
        ),
        ("sync", lambda page: page.click("#sync")),
        ("stats sync", lambda page: page.click("#stats-sync")),
        ("later", lambda page: settle(page, 31000)),
        ("reload", lambda page: page.reload()),
        ("hash swarm", lambda page: page.goto(page.url.split("#")[0] + "#swarm")),
    ]


def serve(root: Path, folder: Path, stack: ExitStack):
    sys.path.insert(0, str(root / "scripts/swarm_ledger"))
    sys.path.insert(1, str(root))
    import ledger_core as core
    import ledger_server
    import new_ledger

    folder.mkdir(parents=True, exist_ok=True)
    stack.enter_context(patch.object(core, "LEDGER_DIR", folder))
    stack.enter_context(patch.object(core, "now_ms", lambda: 1791300000000))
    stack.enter_context(patch.object(new_ledger.secrets, "token_hex", lambda n: "1" * (2 * n)))
    stack.enter_context(patch.object(new_ledger.secrets, "token_urlsafe", lambda n: "1" * 64))
    new_ledger.create(SLUG, INPUTS["content"])
    server = ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
    host = f"127.0.0.1:{server.server_port}"
    stack.enter_context(patch.object(ledger_server, "ALLOWED_HOSTS", {host}))
    stack.enter_context(patch.object(ledger_server, "ALLOWED_ORIGINS", {f"http://{host}"}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stack.callback(thread.join)
    stack.callback(server.server_close)
    stack.callback(server.shutdown)
    return f"http://{host}/{SLUG}"


def record(root: Path, folder: Path) -> list:
    from playwright.sync_api import sync_playwright

    results = []
    with ExitStack() as stack:
        url = serve(root, folder, stack)
        pw = stack.enter_context(sync_playwright())
        browser = pw.chromium.launch()
        stack.callback(browser.close)
        context = browser.new_context(viewport=VIEWPORT, locale="en-US", timezone_id="UTC")
        api = FakeApi()
        context.add_init_script(DETERMINISTIC)
        context.route("**/media/**", api.media)
        context.route("**/artifacts/**", api.artifact)
        context.route("**/api/**", api.handle)
        page = context.new_page()
        page.clock.install(time=START - datetime.timedelta(seconds=10))
        page.clock.pause_at(START)
        page.on("dialog", lambda dialog: (api.dialogs.append(dialog.message), dialog.accept()))
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(url)
        for name, act in steps():
            act(page)
            settle(page)
            requests, dialogs = api.take()
            results.append(
                {
                    "step": name,
                    "page": page.evaluate(CAPTURE),
                    "requests": requests,
                    "dialogs": dialogs,
                    "errors": errors[:],
                }
            )
            errors.clear()
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("folder", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(record(args.root.resolve(), args.folder), indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
