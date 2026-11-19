import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = """
import os
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def answer(self) -> None:
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = (sys.argv[1] + ':' + self.command).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Proof-Service', sys.argv[1])
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    do_GET = do_HEAD = do_POST = do_PUT = answer

server = ThreadingHTTPServer(('0.0.0.0', int(sys.argv[2])), Handler)
if os.environ.get('LEDGER_TLS_CERT') and os.environ.get('LEDGER_TLS_KEY'):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(os.environ['LEDGER_TLS_CERT'], os.environ['LEDGER_TLS_KEY'])
    server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
"""
CLIENT = """
import json
import time
import urllib.error
import urllib.request

results = {}
for method in ('GET', 'HEAD', 'POST', 'PUT'):
    deadline = time.monotonic() + 20
    request = urllib.request.Request('http://127.0.0.1:8765/probe', method=method)
    while True:
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                results[method] = [response.status, response.headers.get('X-Proof-Service'), response.read().decode()]
            break
        except urllib.error.HTTPError as error:
            results[method] = [error.code, 'upstream error']
            break
        except urllib.error.URLError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
print(json.dumps(results))
"""


def compose(command: list[str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*command, *args], capture_output=True, text=True, timeout=180)


def scratch_stack(folder: Path, tls: bool, ca_source: str) -> dict[str, Any]:
    env = dict(os.environ)
    env.update(
        HOME=str(folder),
        LEDGER_CODE=str(ROOT),
        LEDGER_DIR=str(folder),
        LEDGER_PORT="8765",
        LEDGER_READ_PORT="18765",
        LEDGER_API_PORT="8766",
        LEDGER_TLS_CERT=str(folder / "cert.pem") if tls else "",
        LEDGER_TLS_KEY=str(folder / "key.pem") if tls else "",
        LEDGER_CA_FILE=str(folder / "cert.pem") if ca_source == "file" else "",
        LEDGER_CA_PEM=(folder / "cert.pem").read_text() if ca_source == "pem" else "",
        AGENTIHOOKS_SWARM_DATABASE_URL="",
        AGENTIHOOKS_SWARM_REDIS_URL="",
    )
    rendered = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "deploy/compose/ledger.yaml"), "config", "--format", "json"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    stack = json.loads(rendered.stdout)
    stack.pop("name", None)
    services = stack["services"]
    for name, port in (("ledger", "18765"), ("api", "8766")):
        service = services[name]
        for key in ("network_mode", "pid", "user", "healthcheck", "restart"):
            service.pop(key, None)
        service["healthcheck"] = {
            "test": [
                "CMD",
                "python",
                "-c",
                f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=2).close()",
            ],
            "interval": "1s",
            "timeout": "3s",
            "retries": 30,
        }
        service["command"] = ["python", "-u", "-c", BACKEND, name, port]
        service["working_dir"] = str(folder)
        service["user"] = f"{os.getuid()}:{os.getgid()}"
        service["volumes"] = [{"type": "bind", "source": str(folder), "target": str(folder), "read_only": True}]
    services["api"]["network_mode"] = "service:ledger"
    front = services["front"]
    front["network_mode"] = "service:ledger"
    front.pop("restart")
    template = folder / "front.conf.template"
    shutil.copyfile(ROOT / "deploy/compose/ledger-local-front.conf.template", template)
    front["volumes"] = [
        {
            "type": "bind",
            "source": str(template),
            "target": "/etc/nginx/templates/default.conf.template",
            "read_only": True,
        }
    ]
    return stack


@pytest.mark.feature("local-compose-front-service-tls")
@pytest.mark.parametrize(
    "tls,ca_source", [(False, ""), (True, "file"), (True, "pem")], ids=["http", "https-ca-file", "https-ca-pem"]
)
def test_local_compose_front_reads_and_writes_with_service_tls(tmp_path: Path, tls: bool, ca_source: str) -> None:
    with tempfile.TemporaryDirectory(
        prefix="ledger-tls-", dir=os.environ.get("RUNNER_TEMP", str(tmp_path))
    ) as directory:
        folder = Path(directory)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
                "-keyout",
                str(folder / "key.pem"),
                "-out",
                str(folder / "cert.pem"),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        stack = scratch_stack(folder, tls, ca_source)
        compose_file = folder / "compose.json"
        compose_file.write_text(json.dumps(stack))
        command = ["docker", "compose", "-p", f"ledger-tls-{uuid4().hex[:12]}", "-f", str(compose_file)]
        try:
            started = compose(command, "up", "-d")
            assert started.returncode == 0, started.stderr
            probe = compose(command, "exec", "-T", "ledger", "python", "-c", CLIENT)
            healthcheck = json.loads(
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(ROOT / "deploy/compose/ledger.yaml"),
                        "config",
                        "--no-interpolate",
                        "--format",
                        "json",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                ).stdout
            )["services"]["api"]["healthcheck"]["test"]
            health = compose(command, "exec", "-T", "api", *healthcheck[1:])
            print(f"TLS={tls}: front={probe.stdout.strip()} api_health_exit={health.returncode} {health.stderr}")
            assert probe.returncode == 0, probe.stderr
            assert json.loads(probe.stdout) == {
                "GET": [200, "ledger", "ledger:GET"],
                "HEAD": [200, "ledger", ""],
                "POST": [200, "api", "api:POST"],
                "PUT": [200, "api", "api:PUT"],
            }
            assert health.returncode == 0, health.stderr
        finally:
            logs = compose(command, "logs", "--no-color", "--tail", "20")
            print(logs.stdout)
            stopped = compose(command, "down", "--volumes", "--remove-orphans")
            assert stopped.returncode == 0, stopped.stderr
