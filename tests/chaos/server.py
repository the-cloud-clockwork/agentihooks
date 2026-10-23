import shutil
import socket
import subprocess
import time
from pathlib import Path


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


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
        stop = time.monotonic() + deadline_s
        while time.monotonic() < stop:
            if self.process.poll() is not None:
                raise RuntimeError(f"nats-server exited with {self.process.returncode}")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"nats-server did not listen on {self.port}")

    def kill(self) -> None:
        self.process.kill()
        self.process.wait(10)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(10)
