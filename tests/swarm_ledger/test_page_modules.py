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
import new_ledger  # noqa: E402

from scripts.swarm_ledger import ledger_core as core  # noqa: E402
from scripts.swarm_ledger import ledger_server as server  # noqa: E402
from tests.swarm_ledger.ledger_page import ORDER, page_source  # noqa: E402

MODULES = core.TEMPLATE.parent / "static" / "js"
CONTENT = {"title": "Modules", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}


@pytest.fixture
def base():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    host = f"127.0.0.1:{httpd.server_port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    with patch.object(server, "ALLOWED_HOSTS", {host}):
        thread.start()
        yield f"http://{host}"
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers["Content-Type"], resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers["Content-Type"], exc.read().decode()


def imports(name):
    return re.findall(r'^import \{[^}]*\} from "\./([\w]+\.js)";$', (MODULES / name).read_text(), re.M)


def test_the_page_loads_its_script_as_a_module_under_the_page_version():
    page = new_ledger.render(new_ledger.build_doc(CONTENT), "modules", 8765)
    version = core.page_version()
    assert f'<script type="module" src="/static/{version}/js/main.js"></script>' in page
    assert page.count("<script>") == 1
    assert f"<script>{core.TOOLTIPS.read_text()}</script>" in page


def test_every_module_the_page_imports_exists_and_no_module_holds_the_whole_script():
    seen, todo = set(), ["main.js"]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo += imports(name)
    assert seen == {p.name for p in MODULES.glob("*.js")}
    assert len(seen) >= 8
    assert max(len((MODULES / n).read_text().splitlines()) for n in seen) < 400


def test_the_source_view_for_text_checks_covers_every_module():
    assert set(ORDER) == {p.stem for p in MODULES.glob("*.js")}
    assert page_source().count("<script>") == 2


def test_the_server_serves_each_module_at_the_current_version(base):
    version = core.page_version()
    for path in MODULES.glob("*.js"):
        status, ctype, body = get(f"{base}/static/{version}/js/{path.name}")
        assert (status, ctype, body) == (200, "text/javascript; charset=utf-8", path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "route",
    [
        "/static/000000000000/js/main.js",
        "/static/{v}/js/missing.js",
        "/static/{v}/js/../ledger_core.py",
        "/static/{v}/main.js",
    ],
)
def test_the_server_refuses_a_stale_version_or_an_unknown_file(base, route):
    status, _, _ = get(base + route.format(v=core.page_version()))
    assert status == 404


def test_a_module_edit_changes_the_page_version(tmp_path):
    before = core.page_version()
    for path in MODULES.glob("*.js"):
        (tmp_path / path.name).write_text(path.read_text())
    (tmp_path / "chat.js").write_text((tmp_path / "chat.js").read_text() + ";")
    with patch.object(core, "MODULES", tmp_path):
        assert core.page_version() != before
