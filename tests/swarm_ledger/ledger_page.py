import json
from pathlib import Path
from urllib.parse import urlsplit

MODULES = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "static" / "js"
PAGE_URL = "http://127.0.0.1:9/ledger"
ORDER = (
    "config",
    "dom",
    "api",
    "patch",
    "state",
    "sync",
    "markdown",
    "media",
    "artifacts",
    "threads",
    "render",
    "notices",
)
ORDER += ("outline", "folds", "layout", "chat", "swarm", "controls", "events", "main")


def serve_modules(target):
    target.route(
        "**/static/*/js/*.js",
        lambda route: route.fulfill(
            body=(MODULES / route.request.url.rsplit("/", 1)[1]).read_text(encoding="utf-8"),
            content_type="text/javascript; charset=utf-8",
        ),
    )


def snapshot_body(ledger=None, swarm=None, workspaces=None):
    data = {"ledger": ledger, "swarm": swarm, "workspaces": workspaces or {}}
    return f"id: c0\nevent: snapshot\ndata: {json.dumps(data)}\n\n"


def is_events(url):
    path = urlsplit(url).path
    return path.startswith("/api/v1/ledgers/") and path.endswith("/events")


def fulfill_events(route, ledger=None, swarm=None):
    """Answer the page's event stream with one snapshot; the page reconnects after the body ends."""
    route.fulfill(body=snapshot_body(ledger, swarm), content_type="text/event-stream")


def show(tab, html, url=PAGE_URL):
    tab.route(url, lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"))
    serve_modules(tab)
    tab.goto(url)


def page_source():
    template = (MODULES.parents[1] / "template.html").read_text(encoding="utf-8")
    body = []
    for name in ORDER:
        for line in (MODULES / f"{name}.js").read_text(encoding="utf-8").splitlines():
            if not line.startswith("import "):
                body.append(("  " + line.removeprefix("export ")) if line else "")
    script = "<script>\n(() => {\n" + "\n".join(body).strip("\n") + "\n})();\n</script>\n"
    return template.replace('<script type="module" src="/static/__LEDGER_PAGE__/js/main.js"></script>\n', script)
