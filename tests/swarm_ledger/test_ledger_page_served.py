import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace

from tests.swarm_ledger.ledger_page import served


def test_a_served_ledger_page_finishes_its_requests_before_the_test_goes_on():
    started, finished = threading.Event(), threading.Event()

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            started.set()
            time.sleep(0.5)
            self.send_response(204)
            self.end_headers()
            finished.set()

    server = SimpleNamespace(Handler=Slow, ALLOWED_HOSTS=set(), ALLOWED_ORIGINS=set())
    with served(server) as base:
        client = threading.Thread(target=urllib.request.urlopen, args=(base,))
        client.start()
        assert started.wait(5)
    assert finished.is_set()
    client.join(5)
