import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm_ledger import ledger_server as server  # noqa: E402

SLUG = "static-shell"
CONTENT = {
    "title": "Shell <ledger>",
    "overview": "secret record overview",
    "sources": [],
    "phases": [{"title": "phase record title"}],
    "questions": [],
    "followups": [],
}
POLICY = {
    "default-src": "'none'",
    "script-src": "'self'",
    "style-src": "'self'",
    "img-src": "'self'",
    "connect-src": "'self'",
    "base-uri": "'none'",
    "form-action": "'self'",
    "frame-ancestors": "'none'",
    "object-src": "'none'",
}


@pytest.fixture(scope="module")
def base(ledger_dir):
    if not core.paths(SLUG)[1].exists():
        new_ledger.create(SLUG, CONTENT)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    host = f"127.0.0.1:{httpd.server_port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    with patch.object(server, "ALLOWED_HOSTS", {host}):
        thread.start()
        yield f"http://{host}"
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def get(url, **headers):
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


def policy(headers):
    return dict(part.strip().split(" ", 1) for part in headers["Content-Security-Policy"].split(";"))


@pytest.mark.parametrize("path", [f"/{SLUG}", "/", "/?view=bin"])
def test_every_page_shell_carries_the_enforced_policy_and_no_inline_code(base, path):
    status, headers, body = get(base + path)
    assert status == 200
    assert policy(headers) == POLICY
    assert "unsafe" not in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert re.findall(r"<script(?![^>]*\ssrc=)[^>]*>", body) == []
    assert "<style" not in body
    assert not re.search(r"\sstyle=|\son[a-z]+=", body)
    assert "ledger-data" not in body


def test_the_ledger_shell_holds_metadata_but_no_record_seed(base):
    _, _, body = get(f"{base}/{SLUG}")
    token = core.read_token(core.paths(SLUG)[0].read_text())
    assert f'<meta name="ledger-token" content="{token}">' in body
    assert f'<meta name="ledger-page" content="{core.page_version()}">' in body
    assert "<title>Shell &lt;ledger&gt;</title>" in body
    assert "secret record overview" not in body
    assert "phase record title" not in body
    assert f'<script type="module" src="/static/{core.page_version()}/js/main.js"></script>' in body


def test_shell_size_does_not_grow_with_the_record(base):
    before = len(get(f"{base}/{SLUG}")[2])
    server.repository.apply_ops(
        SLUG, ops=[{"op": "add", "thread": "notes", "id": f"n-{n}", "text": "x" * 500} for n in range(40)]
    )
    assert len(get(f"{base}/{SLUG}")[2]) == before


def test_serving_the_shell_never_writes_the_record_files(base):
    html_path, json_path = core.paths(SLUG)
    html_path.write_text(html_path.read_text().replace(core.page_version(), "000000000000"))
    stamps = (html_path.stat().st_mtime_ns, json_path.stat().st_mtime_ns)
    get(f"{base}/{SLUG}")
    get(base + "/")
    assert (html_path.stat().st_mtime_ns, json_path.stat().st_mtime_ns) == stamps


def test_home_shell_lists_no_ledger_rows(base):
    _, _, body = get(base + "/")
    assert SLUG not in body
    assert '<ul id="rows"></ul>' in body
    assert '<div class="watermark" aria-hidden="true"></div>' in body
    assert re.findall(r'data-sort="(\w+)"', body) == ["kind", "open", "done", "swarm", "at"]


@pytest.mark.parametrize("name", sorted(core.static_assets()))
def test_each_asset_is_served_immutable_under_the_page_version(base, name):
    status, headers, body = get(f"{base}/static/{core.page_version()}/{name}")
    assert status == 200
    assert body == core.static_assets()[name].read_text(encoding="utf-8")
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert headers["Content-Type"].startswith("text/css" if name.endswith(".css") else "text/javascript")


@pytest.mark.parametrize(
    "route",
    [
        "/static/000000000000/js/main.js",
        "/static/{v}/js/missing.js",
        "/static/{v}/js/../ledger_core.py",
        "/static/{v}/main.js",
        "/static/{v}/css/../../palette.css",
    ],
)
def test_a_stale_version_or_unknown_asset_is_refused(base, route):
    status, _, body = get(base + route.format(v=core.page_version()))
    assert (status, body) == (404, "no such asset")


def test_an_asset_edit_changes_the_version_the_shell_links(tmp_path):
    before = core.page_version()
    sheet = tmp_path / "ledger.css"
    sheet.write_text("body{}")
    assets = {**core.static_assets(), "css/ledger.css": sheet}
    with patch.object(core, "static_assets", return_value=assets):
        assert core.page_version() != before


def test_the_task_work_folder_reads_on_demand(base, monkeypatch):
    token = core.read_token(core.paths(SLUG)[0].read_text())
    monkeypatch.setattr(server, "workspace_tails", lambda slug, task: {"progress": f"{slug} {task} line"})
    status, _, body = get(f"{base}/api/v1/ledgers/{SLUG}/tasks/t1/workspace", **{"X-Ledger-Token": token})
    assert status == 200
    assert json.loads(body)["data"] == {"progress": f"{SLUG} t1 line"}


def test_an_unknown_work_folder_answers_not_found(base, monkeypatch):
    token = core.read_token(core.paths(SLUG)[0].read_text())

    def refuse(slug, task):
        raise ValueError("bad id")

    monkeypatch.setattr(server, "workspace_tails", refuse)
    status, _, body = get(f"{base}/api/v1/ledgers/{SLUG}/tasks/t1/workspace", **{"X-Ledger-Token": token})
    assert status == 404
    assert json.loads(body)["error"]["code"] == "resource_missing"


def test_the_ledger_rows_carry_their_swarm_state(base, monkeypatch):
    monkeypatch.setattr(server, "swarm_state", lambda slug: "running" if slug == SLUG else None)
    _, _, body = get(base + "/api/v1/ledgers")
    row = next(r for r in json.loads(body)["data"] if r["slug"] == SLUG)
    assert row["swarm"] == "running"


def test_the_event_snapshot_carries_only_the_ledger_and_swarm(monkeypatch):
    monkeypatch.setattr(server, "swarm_status", lambda slug, ledger=None: {"state": "running"})
    assert set(server.stream_resources(SLUG)) == {"ledger", "swarm"}
