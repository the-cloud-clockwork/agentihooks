import signal
import threading
from http.server import ThreadingHTTPServer

from scripts.swarm_ledger.events import Hub

DRAIN_S = 8.0  # under the swarm watchdog's ten second kill wait


class DrainingServer(ThreadingHTTPServer):
    replacing = False

    def __init__(self, address, handler, bind_and_activate=True):
        self.active = 0
        self.idle = threading.Condition()
        super().__init__(address, handler, bind_and_activate)

    def process_request(self, request, client_address):
        with self.idle:
            self.active += 1
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.idle:
                self.active -= 1
                self.idle.notify_all()

    def stop_on(self, signum: int) -> None:
        signal.signal(signum, lambda *_: threading.Thread(target=self.shutdown, daemon=True).start())

    def replace(self) -> None:
        self.replacing = True
        self.shutdown()

    def drain(self, hub: Hub, timeout: float = DRAIN_S) -> bool:
        self.server_close()
        hub.stop()
        with self.idle:
            return self.idle.wait_for(lambda: not self.active, timeout)
