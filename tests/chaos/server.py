import shutil
import subprocess
from pathlib import Path

from tests.events.nats_server import free_port, wait_listening


class NatsServer:
    def __init__(self, store: Path, log: Path, binary: str | None = None):
        self.binary = binary or shutil.which("nats-server")
        if self.binary is None:
            raise RuntimeError("nats-server is not on PATH")
        self.store, self.log, self.port = store, log, free_port()
        self.url = f"nats://127.0.0.1:{self.port}"
        self.process = None

    def start(self, deadline_s: float = 20) -> None:
        argv = [self.binary, "-js", "-a", "127.0.0.1", "-p", str(self.port), "-sd", str(self.store)]
        with open(self.log, "a") as out:
            self.process = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT)
        wait_listening(self.port, self.process, deadline_s)

    def kill(self) -> None:
        self.process.kill()
        self.process.wait(10)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(10)
