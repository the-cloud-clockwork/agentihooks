import http.server
import json
import threading
from pathlib import Path

import pytest

from scripts.swarm_ledger import ledger_link


@pytest.fixture
def server(monkeypatch, tmp_path):
    served = {}

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = served["body"] if "body" in served else json.dumps({"dir": served["dir"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("LEDGER_HOST", "127.0.0.1")
    monkeypatch.setenv("LEDGER_PORT", str(httpd.server_address[1]))
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "scratch-ledger"))
    (tmp_path / "scratch-ledger").mkdir()
    yield served
    httpd.shutdown()
    httpd.server_close()


def link(slug):
    return f"Ledger page: {ledger_link.base()}/{slug} (open it to follow and steer the work)"


def test_a_server_serving_another_folder_gets_no_link(server, tmp_path):
    home = str(Path.home() / "development-ledger")
    server["dir"] = home
    assert ledger_link.page_line("my-plan") == (
        f"No ledger page link: {ledger_link.base()} serves {home}, not {tmp_path / 'scratch-ledger'}. "
        f"Set a spare LEDGER_PORT and start the ledger server with {ledger_link.START}"
    )


def test_a_server_serving_the_ledger_folder_gets_the_link(server, tmp_path):
    server["dir"] = str(tmp_path / "scratch-ledger")
    assert ledger_link.page_line("my-plan") == link("my-plan")


def test_a_server_reporting_a_link_to_the_ledger_folder_gets_the_link(server, tmp_path):
    (tmp_path / "alias").symlink_to(tmp_path / "scratch-ledger")
    server["dir"] = str(tmp_path / "alias")
    assert ledger_link.page_line("my-plan") == link("my-plan")


def test_a_ledger_folder_given_through_a_link_gets_the_link(server, tmp_path, monkeypatch):
    (tmp_path / "alias").symlink_to(tmp_path / "scratch-ledger")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "alias"))
    server["dir"] = str(tmp_path / "scratch-ledger")
    assert ledger_link.page_line("my-plan") == link("my-plan")


def test_a_silent_server_gets_the_link_and_the_start_command(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGER_HOST", "127.0.0.1")
    monkeypatch.setenv("LEDGER_PORT", "1")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    assert ledger_link.page_line("my-plan") == (
        f"{link('my-plan')}. The ledger server is not answering: start it with {ledger_link.START}"
    )


def test_serving_names_the_folder_the_server_reports(server):
    server["dir"] = "/srv/other"
    assert ledger_link.serving() == "/srv/other"


def test_serving_is_none_for_a_reply_that_is_not_json(server):
    server["body"] = b"not json"
    assert ledger_link.serving() is None


def test_serving_is_none_when_nothing_listens(monkeypatch):
    monkeypatch.setenv("LEDGER_HOST", "127.0.0.1")
    monkeypatch.setenv("LEDGER_PORT", "1")
    assert ledger_link.serving() is None


def test_the_ledger_folder_defaults_to_the_home_ledger(monkeypatch):
    monkeypatch.delenv("LEDGER_DIR", raising=False)
    assert ledger_link.folder() == Path.home() / "development-ledger"
    assert ledger_link.folder({"LEDGER_DIR": "~/elsewhere"}) == Path.home() / "elsewhere"
