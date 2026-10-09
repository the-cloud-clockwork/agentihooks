import argparse
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path


def docker(*args, timeout=30):
    return subprocess.check_output(["docker", *args], text=True, timeout=timeout).strip()


def state(container):
    return json.loads(docker("inspect", "--format", "{{json .State}}", container))


def snapshot(container):
    code = "import json; from pathlib import Path; r=Path('/home/worker/current.json'); a=Path(json.loads(r.read_text())['attempt']); c=list((a/'run/supervision').glob('*/context.json')); print(json.dumps({'root':str(c[0].parent),'running':(c[0].parent/'running.json').exists(),'grandchildren':len(list(c[0].parent.glob('fixture-grandchild*.json')))}))"
    return json.loads(docker("exec", container, "python", "-c", code))


def ready(container):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not state(container)["Running"]:
            raise AssertionError("container exited before readiness: " + docker("logs", container)[-1000:])
        try:
            observed = snapshot(container)
            if observed["running"] and observed["grandchildren"] == 2:
                return observed
        except (subprocess.CalledProcessError, ValueError, IndexError):
            pass
        time.sleep(0.1)
    raise AssertionError("container readiness timeout")


def run_case(image, mode, output, kill=False):
    container = docker(
        "run",
        "--detach",
        "--network",
        "none",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        image,
        "python",
        "/opt/fixture/container_case.py",
        mode,
    )
    try:
        observed = ready(container)
        root = observed["root"]
        if kill:
            docker(
                "exec",
                container,
                "python",
                "-c",
                f"import json,os,signal; from pathlib import Path; c=json.loads(Path({root!r}, 'context.json').read_text()); assert c['process_namespace']==os.readlink('/proc/self/ns/pid'); os.kill(c['supervisor_pid'],signal.SIGKILL)",
            )
        else:
            before = docker(
                "exec",
                container,
                "python",
                "-c",
                f"import json; from pathlib import Path; r=Path({root!r}); print(json.dumps({{p.name:json.loads(p.read_text())['tick'] for p in r.glob('heartbeat-*.json')}}))",
            )
            viewer = subprocess.Popen(
                ["docker", "attach", "--no-stdin", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(0.2)
            viewer.terminate()
            viewer.wait(timeout=5)
            after = docker(
                "exec",
                container,
                "python",
                "-c",
                f"import json; from pathlib import Path; r=Path({root!r}); print(json.dumps({{p.name:json.loads(p.read_text())['tick'] for p in r.glob('heartbeat-*.json')}}))",
            )
            initial, final = json.loads(before), json.loads(after)
            assert len(initial) == 5 and all(final[k] > v for k, v in initial.items())
            docker("kill", "--signal", "TERM", container)
        start = time.monotonic()
        code = int(docker("wait", container, timeout=12))
        elapsed = time.monotonic() - start
        stopped = state(container)
        assert not stopped["Running"] and stopped["Pid"] == 0
        target = output / uuid.uuid4().hex
        target.mkdir()
        docker("cp", f"{container}:/home/worker/attempts", str(target))
        results = list(target.glob("attempts/*/run/supervision/*/result.json"))
        if kill:
            assert code == 137 and not results
            result = {"checkpoint_status": "absent", "supervisor_child_exit_total": "unavailable after abrupt death"}
        else:
            assert len(results) == 1
            result = json.loads(results[0].read_text())
            expected = "complete" if mode == "complete" else "incomplete"
            assert result["checkpoint_status"] == expected
            assert code == (0 if mode == "complete" else 75)
        return {
            "mode": mode,
            "kill": kill,
            "exit_code": code,
            "elapsed_shutdown_seconds": round(elapsed, 3),
            "container_process_tree_gone": True,
            "viewer_detached": not kill,
            "result": result,
        }
    finally:
        docker("rm", "--force", container)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--prior-image", required=True)
    parser.add_argument("--tested-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    positive = [run_case(args.image, "complete", args.output) for _ in range(2)]
    negative = run_case(args.image, "complete", args.output, kill=True)
    recovery = [run_case(args.image, mode, args.output) for mode in ("late", "missing", "forced")]
    prior = json.loads(
        docker(
            "run", "--rm", "--network", "none", args.prior_image, "python", "/opt/swarm-node/worker_image.py", "report"
        )
    )
    rollback = {
        "selected_prior_image_for_new_attempt": True,
        "inventory": prior["observed"],
        "active_ownership_replaced": False,
        "existing_homes_deleted": False,
    }
    fixture = Path(__file__).parent
    manifest = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (fixture / "process_fixture.py", fixture / "container_case.py", fixture / "Dockerfile")
    }
    shared = {
        "package": "SV2-IMG-03",
        "tested_commit": args.tested_commit,
        "mocked": False,
        "fixture_manifest": manifest,
        "startup_network": "none",
        "production_rollout": "not exercised",
    }
    for case, outcomes in (("a", positive), ("b", [negative]), ("c", recovery)):
        (args.output / f"{case}-result.json").write_text(
            json.dumps({**shared, "status": "passed", "outcomes": outcomes}, indent=2) + "\n"
        )
    (args.output / "result.json").write_text(
        json.dumps(
            {
                **shared,
                "status": "passed",
                "cases": ["A", "B", "C"],
                "rollback_rehearsal": rollback,
                "authority": "synthetic controller registration for fixture only",
                "state": {
                    "observed": "real isolated containers and real headless herdr",
                    "accepted": "fixture assertions passed",
                    "committed": "tested commit",
                    "externally_verified": "production rollout and merge not run",
                },
            },
            indent=2,
        )
        + "\n"
    )
    print("SV2-IMG-03 container acceptance and rollback passed", flush=True)


if __name__ == "__main__":
    main()
