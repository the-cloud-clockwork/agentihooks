"""Live SV2-HDR-02 proof: the worker sshd from docker/swarm-node/ssh and the pinned herdr behind it on localhost.

Usage: PYTHONPATH=<worktree> python tests/integration/herdr_terminal/terminal_proof.py WORK_DIR
Worker attempt trees live in one short folder directly under $HOME, removed at exit, because the herdr client
socket path under an attempt home must fit the 107 byte unix socket limit; results and client homes stay in WORK_DIR.
"""

import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.herdr import capabilities
from scripts.swarm_v2.herdr.attach import ClientHome, Terminal
from scripts.swarm_v2.herdr.endpoints import (
    Endpoint,
    EndpointRefused,
    HostIdentity,
    TerminalAuthority,
    grant_scope,
    operator_scope,
)
from scripts.swarm_v2.operator_auth import OPERATOR

REPO = Path(__file__).resolve().parents[3]
FIXTURE = json.loads((REPO / "tests/fixtures/swarm_v2/herdr-terminal.json").read_text())
NAMESPACE = FIXTURE["namespace"]
EXE_A = FIXTURE["registration"]["execution_id"]
EXE_B = FIXTURE["other_execution"]["execution_id"]
EXE_C = "exe-" + "c" * 32
PORT = int(os.environ.get("TERMINAL_PROOF_PORT", "22506"))
HERDR = Path(os.environ.get("TERMINAL_PROOF_HERDR") or shutil.which("herdr")).resolve()
USER = getpass.getuser()
TOOLS = f"{HERDR.parent}:/usr/bin:/bin"

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
WORKERS = Path(tempfile.mkdtemp(prefix="v", dir=Path.home()))
ATTEMPTS = WORKERS / "x"
rows: list[dict] = []
failures = 0


def scrub(text: str) -> str:
    return text.replace(str(WORKERS), "<workers>").replace(str(root), "<work>").replace(USER, "<user>")


def record(step: str, held: bool, detail: object) -> None:
    global failures
    failures += 0 if held else 1
    rows.append({"step": step, "held": held, "detail": json.loads(scrub(json.dumps(detail)))})
    print(f"{step} {'ok' if held else 'FAILED'}", flush=True)


def refused(action) -> str:
    try:
        action()
    except EndpointRefused as exc:
        return exc.reason
    return ""


def write(path: Path, text: str | bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as handle:
        handle.write(text if isinstance(text, bytes) else text.encode())


class Worker:
    """One execution attempt's sshd and supervised herdr server, laid out the way the worker Pod lays them out."""

    def __init__(self, name: str, exe: str, identity: HostIdentity, user_ca: str) -> None:
        self.base = WORKERS / name
        self.exe, self.identity, self.user_ca = exe, identity, user_ca
        self.attempts = ATTEMPTS
        attempt = self.attempts / exe
        self.home = attempt / "homes" / "codex"
        self.run = attempt / "run"
        self.home.mkdir(parents=True, exist_ok=True)
        self.run.mkdir(mode=0o700, exist_ok=True)
        authority = {
            "execution_id": exe,
            "generation": 1,
            "task_id": "proof",
            "seat_id": "eng-1",
            "swarm_id": "fixture-swarm",
            "grant_id": "lgr-" + "d" * 32,
        }
        write(attempt / "execution.json", json.dumps({"attempt": exe, "homes": {"codex": "homes/codex"}}))
        write(attempt / "registration.json", json.dumps(authority))
        self.launch = self.base / "launch.json"
        launch = {"schema_version": 1, "authority": authority, "harness": "codex", "agent": ["true"]}
        write(self.launch, json.dumps(launch))
        python = self.base / "python"
        write(python, f'#!/bin/sh\nPYTHONPATH="{REPO}" exec "{sys.executable}" "$@"\n', 0o755)
        self.material = self.base / "material"
        self.state = self.base / "state"
        self.environ = {
            "HOME": str(self.base),
            "PATH": "/usr/bin:/bin",
            "SWARM_TERMINAL_MATERIAL": str(self.material),
            "SWARM_TERMINAL_STATE": str(self.state),
            "SWARM_TERMINAL_PORT": str(PORT),
            "SWARM_TERMINAL_LISTEN": "127.0.0.1",
            "SWARM_TERMINAL_ATTEMPTS": str(self.attempts),
            "SWARM_TERMINAL_LAUNCH": str(self.launch),
            "SWARM_TERMINAL_PYTHON": str(python),
            "SWARM_TERMINAL_PATH": TOOLS,
        }

    def mount(self, identity: HostIdentity) -> None:
        write(self.material / "host_key", identity.private, 0o600)
        write(self.material / "user_ca.pub", f"{self.user_ca}\n")
        write(self.material / "principals", f"{self.exe}\n")

    def start_sshd(self, identity: HostIdentity) -> subprocess.CompletedProcess:
        self.mount(identity)
        done = subprocess.run(
            [str(REPO / "docker/swarm-node/ssh/start.sh")], env=self.environ, capture_output=True, text=True
        )
        for _ in range(100):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", PORT)) == 0:
                    break
            time.sleep(0.1)
        return done

    def stop_sshd(self) -> None:
        pid = self.state / "sshd.pid"
        if pid.exists():
            os.kill(int(pid.read_text()), 15)
            for _ in range(50):
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", PORT)) != 0:
                        break
                time.sleep(0.1)
            pid.unlink(missing_ok=True)

    def herdr_env(self) -> dict:
        return {"HOME": str(self.home), "XDG_RUNTIME_DIR": str(self.run), "PATH": TOOLS, "TERM": "dumb", "USER": USER}

    def start_herdr(self) -> bool:
        log = (self.base / "herdr-server.log").open("w")
        subprocess.Popen(
            [str(HERDR), "server"],
            env=self.herdr_env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        for _ in range(100):
            status = subprocess.run(
                [str(HERDR), "status", "server", "--json"], env=self.herdr_env(), capture_output=True, text=True
            )
            if '"running":true' in status.stdout:
                return True
            time.sleep(0.2)
        return False

    def stop_herdr(self) -> None:
        subprocess.run([str(HERDR), "server", "stop"], env=self.herdr_env(), capture_output=True, timeout=30)

    def sshd_effective(self) -> dict:
        done = subprocess.run(
            ["/usr/sbin/sshd", "-T", "-f", str(self.state / "sshd_config")], capture_output=True, text=True
        )
        wanted = (
            "port",
            "listenaddress",
            "authorizedkeysfile",
            "passwordauthentication",
            "kbdinteractiveauthentication",
            "permitrootlogin",
            "allowtcpforwarding",
            "allowstreamlocalforwarding",
            "allowagentforwarding",
            "x11forwarding",
            "permittunnel",
            "permituserenvironment",
            "trustedusercakeys",
            "authorizedprincipalsfile",
            "strictmodes",
        )
        found = dict(line.split(" ", 1) for line in done.stdout.splitlines() if " " in line)
        return {key: found.get(key) for key in wanted}


served: dict[str, str] = {}
ENVIRONMENT = (
    "live on a workstation, not Anton: the image sshd template and start script and the session entry run "
    "unprivileged on 127.0.0.1 with the pinned herdr behind them; the cluster DNS name is answered by an injected "
    "resolver mapping it to 127.0.0.1, except the missing route step, which uses the system resolver, and the "
    "public route step, whose injected resolver answers 1.1.1.1"
)


def lookup(host, port, type):  # noqa: A002
    if host not in served:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return [(socket.AF_INET, type, 6, "", (served[host], port))]


def client(name: str, authority: TerminalAuthority, resolver=lookup) -> Terminal:
    home = ClientHome(root / name, USER, PORT)
    base = {"HOME": str(root / f"{name}-home"), "PATH": TOOLS, "USER": USER, "TERM": "dumb"}
    return Terminal(home, authority, NAMESPACE, capabilities.runner(home.environment(base)), resolver)


def grant(exe: str, generation: int = 1):
    return grant_scope(Registration(**(FIXTURE["registration"] | {"execution_id": exe, "generation": generation})))


def run(terminal: Terminal, argv: list[str]) -> subprocess.CompletedProcess:
    return terminal.run(argv)


def main() -> int:
    nonce = uuid.uuid4().hex[:12]
    authority = TerminalAuthority(Ed25519PrivateKey.generate())
    stranger = TerminalAuthority(Ed25519PrivateKey.generate())
    identity_a = HostIdentity.generate()
    worker_a = Worker("a", EXE_A, identity_a, authority.user_ca)
    workers = [worker_a]
    record("environment", True, ENVIRONMENT)
    try:
        started = worker_a.start_sshd(identity_a)
        record("a_sshd_started", started.returncode == 0 and (worker_a.state / "sshd.pid").exists(), started.stderr)
        effective = worker_a.sshd_effective()
        record(
            "a_sshd_private_and_certificate_only",
            effective
            == {
                "port": str(PORT),
                "listenaddress": f"127.0.0.1:{PORT}",
                "authorizedkeysfile": "none",
                "passwordauthentication": "no",
                "kbdinteractiveauthentication": "no",
                "permitrootlogin": "no",
                "allowtcpforwarding": "no",
                "allowstreamlocalforwarding": "no",
                "allowagentforwarding": "no",
                "x11forwarding": "no",
                "permittunnel": "no",
                "permituserenvironment": "no",
                "trustedusercakeys": str(worker_a.state / "user_ca.pub"),
                "authorizedprincipalsfile": str(worker_a.state / "principals"),
                "strictmodes": "yes",
            },
            effective,
        )
        record("a_herdr_server_started", worker_a.start_herdr(), "supervised herdr server for execution A")
        scope_a = grant(EXE_A)
        endpoint_a = Endpoint(scope_a, NAMESPACE)
        served[endpoint_a.host] = "127.0.0.1"
        operator = client("client-operator", authority)
        verdict = operator.attach(scope_a, identity_a.public)
        record(
            "a_attach_qualified",
            verdict.compatible,
            {"target": verdict.target, "incarnation": verdict.incarnation, "refusals": list(verdict.refusals)},
        )
        create = operator.qualifier.require(endpoint_a.name, endpoint_a.incarnation, "workspace_create")
        made = run(operator, [*create, "--label", f"hdr02-{nonce}", "--cwd", str(worker_a.home), "--no-focus"])
        reply = json.loads(made.stdout)["result"] if made.returncode == 0 else {}
        pane = reply.get("root_pane", {}).get("pane_id", "")
        record(
            "a_workspace_created",
            made.returncode == 0 and bool(pane),
            {"exit": made.returncode, "workspace": reply.get("workspace", {}).get("workspace_id"), "pane": pane},
        )
        launch = operator.qualifier.require(endpoint_a.name, endpoint_a.incarnation, "pane_run")
        ran = run(operator, [*launch, pane, f"echo started-{nonce}; sleep 120"])
        record("a_pane_run", ran.returncode == 0, {"exit": ran.returncode, "pane": pane})
        wait = operator.qualifier.require(endpoint_a.name, endpoint_a.incarnation, "pane_wait_output")
        waited = run(operator, [*wait, pane, "--match", f"started-{nonce}", "--timeout", "10000"])
        matched = f"started-{nonce}" in waited.stdout
        record(
            "a_work_survives_client_exit",
            waited.returncode == 0 and matched,
            {"exit": waited.returncode, "marker_read_by_a_later_client": matched},
        )
        second = client("client-second", authority)
        again = second.attach(scope_a, identity_a.public)
        record(
            "a_second_independent_client", again.compatible, {"target": again.target, "incarnation": again.incarnation}
        )

        unauthorized = client("client-unauthorized", stranger)
        record(
            "b_unauthorized_client",
            refused(lambda: unauthorized.attach(scope_a, identity_a.public)) == "unauthorized",
            unauthorized.terminal_endpoint_auth_failures(),
        )
        endpoint_b = Endpoint(grant(EXE_B), NAMESPACE)
        served[endpoint_b.host] = "127.0.0.1"
        other = client("client-other-execution", authority)
        record(
            "b_certificate_for_another_execution",
            refused(lambda: other.attach(grant(EXE_B), identity_a.public)) == "unauthorized",
            other.terminal_endpoint_auth_failures(),
        )
        resolver = client("client-cluster-dns", authority, socket.getaddrinfo)
        reason = refused(lambda: resolver.attach(scope_a, identity_a.public))
        record(
            "b_missing_cluster_dns_route",
            reason == "no_route" and not resolver.home.root.exists(),
            {"reason": reason, "client_files": resolver.home.root.exists()},
        )
        public = client(
            "client-public-route",
            authority,
            lambda host, port, type: [(socket.AF_INET, type, 6, "", ("1.1.1.1", port))],
        )
        reason = refused(lambda: public.attach(scope_a, identity_a.public))
        record("b_public_route", reason == "public_route", {"reason": reason})
        agent_env = {
            "PATH": TOOLS,
            "HOME": str(root / "client-operator-home"),
            "AGENTIHOOKS_AGENT_NAME": "engineer@proof",
        }
        raw = subprocess.run(
            [sys.executable, "-m", "scripts.swarm_v2.herdr.capabilities", endpoint_a.name, EXE_A],
            env=agent_env | {"PYTHONPATH": str(REPO)},
            capture_output=True,
            text=True,
        )
        record("b_raw_target_refused_for_an_agent", raw.returncode == 2 and raw.stdout == "", raw.stderr.strip())

        worker_a.stop_sshd()
        replaced = HostIdentity.generate()
        worker_a.start_sshd(replaced)
        reason = refused(lambda: operator.attach(scope_a, identity_a.public))
        record("c_changed_host_key_refused", reason == "host_key_changed", {"reason": reason})
        bumped = operator.attach(grant(EXE_A, 2), replaced.public)
        record(
            "c_new_generation_verified",
            bumped.compatible and bumped.incarnation == f"{EXE_A}/2",
            {"target": bumped.target, "incarnation": bumped.incarnation},
        )
        pinned = operator.home.known_hosts(endpoint_a).read_text()
        reason = refused(lambda: operator.attach(scope_a, identity_a.public))
        record(
            "c_stale_generation_cannot_overwrite_the_newer_pin",
            reason == "stale_generation" and operator.home.known_hosts(endpoint_a).read_text() == pinned,
            {"reason": reason, "pin_unchanged": operator.home.known_hosts(endpoint_a).read_text() == pinned},
        )
        worker_a.stop_sshd()
        worker_a.stop_herdr()

        identity_c = HostIdentity.generate()
        worker_c = Worker("c", EXE_C, identity_c, authority.user_ca)
        workers.append(worker_c)
        worker_c.start_sshd(identity_c)
        record("c_replacement_herdr_started", worker_c.start_herdr(), "supervised herdr server for execution C")
        scope_c = operator_scope(OPERATOR, "fixture-swarm", SimpleNamespace(execution_id=EXE_C, generation=1))
        endpoint_c = Endpoint(scope_c, NAMESPACE)
        served[endpoint_c.host] = "127.0.0.1"
        fresh = operator.attach(scope_c, identity_c.public)
        record(
            "c_replacement_verified",
            fresh.compatible and fresh.incarnation == f"{EXE_C}/1",
            {"target": fresh.target, "incarnation": fresh.incarnation},
        )
        replay = operator.attach(scope_c, identity_c.public)
        listed = json.loads(run(operator, ["herdr", "machine", "list", "--json"]).stdout)
        labels = sorted(machine["label"] for machine in listed)
        record(
            "c_replay_adds_no_duplicate",
            replay.compatible and labels == sorted([endpoint_a.name, endpoint_c.name]),
            labels,
        )
        reason = refused(lambda: operator.attach(scope_a, identity_a.public))
        record("c_old_identity_not_accepted", reason == "stale_generation", {"reason": reason})
        record(
            "terminal_endpoint_auth_failures",
            True,
            {
                name: {"auth": found.terminal_endpoint_auth_failures(), "all": found.terminal_endpoint_refusals()}
                for name, found in (
                    ("operator", operator),
                    ("unauthorized", unauthorized),
                    ("other_execution", other),
                    ("cluster_dns", resolver),
                    ("public_route", public),
                )
            },
        )
    finally:
        for worker in workers:
            worker.stop_sshd()
            worker.stop_herdr()
        for key in root.glob("client-*/.ssh/swarm-terminals/id_ed25519"):
            key.unlink()
        shutil.rmtree(WORKERS)
        (root / "results.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    print(f"failures={failures}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
