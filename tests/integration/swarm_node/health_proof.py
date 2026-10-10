import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

from supervision_proof import docker, ready

PORT = 18080
PID_FILE = "/home/worker/brain.pid"
BRAIN = (
    "import os,sys\n"
    "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
    "class H(BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        self.send_response(200 if self.path == '/health' else 404)\n"
    "        self.end_headers()\n"
    "    def log_message(self, *a):\n"
    "        pass\n"
    "server = HTTPServer(('127.0.0.1', int(sys.argv[1])), H)\n"
    "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
    "server.serve_forever()\n"
)
IDENTITY = (
    "import json,sys; from pathlib import Path; r=Path(sys.argv[1]); a=r.parent.parent.parent; "
    "c=json.loads((r/'context.json').read_text()); g=json.loads((r/'agent.json').read_text()); "
    "print(json.dumps({'incarnation':c['incarnation'],'supervisor_pid':c['supervisor_pid'],"
    "'agent_pid':g['pid'],'agent_start':g['pid_start'],'attempt':str(a)}))"
)
PROTECTED = (
    "import hashlib,json,sys; from pathlib import Path; r=Path(sys.argv[1]); a=r.parent.parent.parent; "
    "files=[a/'execution.json',a/'registration.json',a/'launch.json',r/'context.json',r/'running.json']; "
    "h=a/'homes'/'codex'; "
    "print(json.dumps({'files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},"
    "'home':sorted(str(p.relative_to(h)) for p in h.rglob('*') "
    "if p.is_file() and 'herdr' not in p.relative_to(h).parts)}))"
)


def start(image, private):
    return docker(
        "run",
        "--detach",
        "--network",
        "none",
        "--memory",
        "512m",
        "--pids-limit",
        "128",
        "--env",
        f"BRAIN_URL=http://127.0.0.1:{PORT}",
        "--env",
        f"FIXTURE_PRIVATE={private}",
        image,
        "python",
        "/opt/fixture/container_case.py",
        "complete",
    )


def python(container, code, *args):
    return json.loads(docker("exec", container, "python", "-c", code, *args))


def probe(container, attempt, mode):
    done = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "python",
            "/opt/swarm-node/health.py",
            mode,
            "--attempt",
            attempt,
            "--harness",
            "codex",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {"exit_code": done.returncode, "report": json.loads(done.stdout), "stderr": done.stderr}


def runtime(container, root):
    identity = python(container, IDENTITY, root)
    status = json.loads(docker("inspect", "--format", "{{json .State}}", container))
    restarts = docker("inspect", "--format", "{{.RestartCount}}", container)
    return {**identity, "container_started_at": status["StartedAt"], "container_restarts": int(restarts)}


def brain_up(container, attempt):
    docker("exec", "--detach", container, "python", "-c", BRAIN, str(PORT), PID_FILE)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observed = probe(container, attempt, "readiness")
        if observed["report"]["dependencies"]["brain"] == "ok":
            return observed
        time.sleep(0.2)
    raise AssertionError("fixture brain never answered")


def brain_down(container):
    code = "import os,signal,sys; os.kill(int(open(sys.argv[1]).read()), signal.SIGTERM)"
    docker("exec", container, "python", "-c", code, PID_FILE)


def expect(observed, code, status, reason, brain=None):
    report = observed["report"]
    assert observed["exit_code"] == code, observed
    assert report["status"] == status, observed
    assert report["worker_startup_failure_reason"] == reason, observed
    if brain is not None:
        assert report["dependencies"]["brain"] == brain, observed


def remove(container):
    subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=60)


def positive_run(image, private):
    container = start(image, private)
    try:
        root = ready(container)["root"]
        before = runtime(container, root)
        attempt = before["attempt"]
        healthy = brain_up(container, attempt)
        expect(healthy, 0, "ready", None, "ok")
        brain_down(container)
        live = probe(container, attempt, "liveness")
        expect(live, 0, "live", None)
        degraded = probe(container, attempt, "readiness")
        expect(degraded, 0, "degraded", None, "brain_unreachable")
        after = runtime(container, root)
        assert after == before, (before, after)
        return {
            "container": container,
            "healthy": healthy,
            "outage_liveness": live,
            "outage_readiness": degraded,
            "runtime_before": before,
            "runtime_after": after,
        }
    finally:
        remove(container)


def missing_binary_run(image, private):
    container = start(image, private)
    try:
        root = ready(container)["root"]
        attempt = runtime(container, root)["attempt"]
        observed = {mode: probe(container, attempt, mode) for mode in ("startup", "readiness", "diagnose")}
        expect(observed["startup"], 1, "not_ready", "missing_binary:codex")
        expect(observed["readiness"], 1, "not_ready", "missing_binary:codex")
        expect(observed["diagnose"], 0, "not_ready", "missing_binary:codex")
        assert "FIXTURE_PRIVATE" in observed["diagnose"]["report"]["environment"]
        assert private not in json.dumps(observed)
        return {"container": container, "probes": observed}
    finally:
        remove(container)


def private_home_run(image, private):
    container = start(image, private)
    try:
        root = ready(container)["root"]
        attempt = runtime(container, root)["attempt"]
        home = f"{attempt}/homes/codex"
        protected = python(container, PROTECTED, root)
        observed = {}
        for mode, reason in (("0555", "home_unwritable"), ("0000", "home_unreadable")):
            docker("exec", container, "chmod", mode, home)
            observed[mode] = probe(container, attempt, "readiness")
            expect(observed[mode], 1, "not_ready", reason)
        docker("exec", container, "chmod", "0700", home)
        assert python(container, PROTECTED, root) == protected
        corrected = probe(container, attempt, "readiness")
        assert corrected["report"]["checks"]["home"] is None, corrected
        assert private not in json.dumps(observed)
        return {"container": container, "protected": protected, "probes": observed, "corrected": corrected}
    finally:
        remove(container)


def recovery_run(image, private):
    container = start(image, private)
    try:
        root = ready(container)["root"]
        before = runtime(container, root)
        attempt = before["attempt"]
        protected = python(container, PROTECTED, root)
        outage = probe(container, attempt, "readiness")
        expect(outage, 0, "degraded", None, "brain_unreachable")
        restored = brain_up(container, attempt)
        expect(restored, 0, "ready", None, "ok")
        replay = probe(container, attempt, "readiness")
        expect(replay, 0, "ready", None, "ok")
        after = runtime(container, root)
        assert after == before, (before, after)
        after_protected = python(container, PROTECTED, root)
        assert after_protected == protected, (protected, after_protected)
        return {
            "container": container,
            "outage": outage,
            "restored": restored,
            "replay": replay,
            "runtime_before": before,
            "runtime_after": after,
            "protected": protected,
        }
    finally:
        remove(container)


def reasons(value):
    if isinstance(value, dict):
        found = [value["worker_startup_failure_reason"]] if "worker_startup_failure_reason" in value else []
        return found + [r for item in value.values() for r in reasons(item)]
    if isinstance(value, list):
        return [r for item in value for r in reasons(item)]
    return []


def manifest(tested_commit, images):
    repo = Path(__file__).resolve().parents[3]
    inputs = [
        "tests/integration/swarm_node/health_proof.py",
        "tests/integration/swarm_node/run_health_proof.sh",
        "tests/integration/swarm_node/Dockerfile",
        "docker/swarm-node/Dockerfile",
        "docker/swarm-node/health.py",
        "scripts/swarm_v2/worker_health.py",
    ]
    hashes = {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in inputs}
    return {"tested_commit": tested_commit, "images": images, "inputs": hashes}


def record(output, name, case, runs, fixture):
    document = {
        "package": "SV2-IMG-04",
        "case": case,
        "mocked": False,
        "network": "none; fixture brain on container loopback",
        "fixture_manifest": fixture,
        "runs": runs,
        "worker_startup_failure_reason": reasons(runs),
        "status": "passed",
    }
    (output / name).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--missing-binary-image", required=True)
    parser.add_argument("--tested-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    private = hashlib.sha256(f"SV2-IMG-04 {args.tested_commit}".encode()).hexdigest()[:32]
    fixture = manifest(args.tested_commit, {"fixture": args.image, "missing_binary": args.missing_binary_image})
    case = "A"
    try:
        record(args.output, "a-result.json", case, [positive_run(args.image, private) for _ in range(2)], fixture)
        case = "B"
        rejections = [missing_binary_run(args.missing_binary_image, private), private_home_run(args.image, private)]
        record(args.output, "b-result.json", case, rejections, fixture)
        case = "C"
        record(args.output, "c-result.json", case, [recovery_run(args.image, private) for _ in range(2)], fixture)
    except Exception as exc:
        failure = {
            "package": "SV2-IMG-04",
            "case": case,
            "error": repr(exc)[:2000],
            "seed": private,
            "fixture_manifest": fixture,
        }
        (args.output / "failure.json").write_text(json.dumps(failure, indent=2, sort_keys=True) + "\n")
        raise
    versions = json.loads((Path(__file__).resolve().parents[3] / "docker/swarm-node/versions.lock").read_text())
    summary = {
        "package": "SV2-IMG-04",
        "tested_commit": args.tested_commit,
        "mocked": False,
        "stubbed": ["brain: fixture HTTP server on container loopback"],
        "cases": ["A", "B", "C"],
        "status": "passed",
        "fixture_manifest": fixture,
        "supported_versions": {"harnesses": ["claude", "codex"], "worker_image": versions},
        "interfaces": [
            "health.py liveness, startup, readiness and diagnose with exit 0, 1 and 64",
            "worker_startup_failure_reason in every report",
            "dependencies.brain: ok, unconfigured, brain_unreachable, brain_http_<status>",
        ],
        "compatibility": "new local probe interface; no earlier probe protocol exists to compare",
        "state": {
            "observed": "real isolated containers and real headless herdr",
            "accepted": "fixture assertions passed",
            "committed": "tested commit",
            "externally_verified": "not run; production rollout belongs to antoncore",
        },
        "rollback_rehearsal": "deferred to the ledger follow up for the worker Pod template, not performed: probe "
        "wiring and thresholds live in that template, which is not built yet",
        "limitations": [
            "Only the codex harness runs in the container cases; claude is covered by unit tests",
            "Linux amd64 only",
            "Probe thresholds are not chosen here",
        ],
        "production_rollout": "not exercised; probe wiring belongs to the Pod template and antoncore GitOps",
    }
    (args.output / "result.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print("HEALTH ACCEPTANCE PASSED")


if __name__ == "__main__":
    main()
