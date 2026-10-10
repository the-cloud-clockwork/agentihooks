import argparse
import fcntl
import hashlib
import json
import os
import pty
import select
import shlex
import struct
import subprocess
import termios
import time
import uuid
from pathlib import Path


def docker(*args, timeout=30):
    return subprocess.check_output(["docker", *args], text=True, timeout=timeout, stderr=subprocess.STDOUT).strip()


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


def herdr_argv(container, root):
    attempt = str(Path(root).parent.parent.parent)
    return [
        "docker",
        "exec",
        "--env",
        "HERDR_CONFIG_PATH=" + root + "/herdr.toml",
        "--env",
        "HOME=" + attempt + "/homes/codex",
        "--env",
        "XDG_RUNTIME_DIR=" + attempt + "/run",
        container,
        "herdr",
    ]


def terminal_disconnect(container, root):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    command = herdr_argv(container, root)
    command[2:2] = ["--interactive", "--tty", "--env", "TERM=xterm-256color"]
    viewer = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    try:
        readable, _, _ = select.select([master], [], [], 8)
        assert readable and os.read(master, 65536)
        time.sleep(0.2)
        assert viewer.poll() is None
        server = json.loads(subprocess.check_output([*herdr_argv(container, root), "status", "--json"], text=True))
        viewer.terminate()
        viewer.wait(timeout=5)
        return {
            "terminal_transport": "real herdr TUI over Docker PTY",
            "viewer_exit_code": viewer.returncode,
            "server_while_attached": server,
        }
    finally:
        if viewer.poll() is None:
            viewer.kill()
            viewer.wait(timeout=5)
        os.close(master)


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
            start = time.monotonic()
            try:
                docker(
                    "exec",
                    container,
                    "python",
                    "-c",
                    f"import json,os,signal; from pathlib import Path; c=json.loads(Path({root!r}, 'context.json').read_text()); assert c['process_namespace']==os.readlink('/proc/self/ns/pid'); os.kill(c['supervisor_pid'],signal.SIGKILL)",
                )
            except subprocess.CalledProcessError as exc:
                if exc.returncode != 137:
                    raise
        else:
            before = docker(
                "exec",
                container,
                "python",
                "-c",
                f"import json; from pathlib import Path; r=Path({root!r}); print(json.dumps({{p.name:json.loads(p.read_text())['tick'] for p in r.glob('heartbeat-*.json')}}))",
            )
            terminal = terminal_disconnect(container, root)
            time.sleep(0.2)
            after = docker(
                "exec",
                container,
                "python",
                "-c",
                f"import json; from pathlib import Path; r=Path({root!r}); print(json.dumps({{p.name:json.loads(p.read_text())['tick'] for p in r.glob('heartbeat-*.json')}}))",
            )
            initial, final = json.loads(before), json.loads(after)
            assert len(initial) == 5 and all(final[k] > v for k, v in initial.items())
            start = time.monotonic()
            docker("kill", "--signal", "TERM", container)
        code = int(docker("wait", container, timeout=12))
        elapsed = time.monotonic() - start
        stopped = state(container)
        assert not stopped["Running"] and stopped["Pid"] == 0
        target = output / uuid.uuid4().hex
        target.mkdir()
        docker("cp", f"{container}:/home/worker/attempts", str(target))
        results = list(target.glob("attempts/*/run/supervision/*/result.json"))
        launch = json.loads(next(target.glob("attempts/*/launch.json")).read_text())
        bound = launch["quiesce_seconds"] + launch["checkpoint_seconds"] + 3 * launch["kill_seconds"] + 1
        assert elapsed < bound
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
            "shutdown_bound_seconds": bound,
            "container_process_tree_gone": True,
            "viewer_detached": not kill,
            "terminal_disconnect": terminal if not kill else None,
            "result": result,
        }
    finally:
        docker("rm", "--force", container)


def ownership(container, root):
    code = f"import json,os,hashlib; from pathlib import Path; r=Path({root!r}); a=r.parent.parent.parent; c=json.loads((r/'context.json').read_text()); p=Path('/proc',str(c['supervisor_pid']),'stat').read_text().rsplit(')',1)[1].split(); files=[a/'execution.json',a/'registration.json',*sorted((a/'homes').rglob('*'))]; hashes={{str(f.relative_to(a)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.is_file() and not str(f.relative_to(a)).startswith(('homes/codex/.config/herdr/','homes/codex/.local/state/herdr/'))}}; print(json.dumps({{'pid':c['supervisor_pid'],'start':p[19],'namespace':os.readlink('/proc/self/ns/pid'),'hashes':hashes}}))"
    return json.loads(docker("exec", container, "python", "-c", code))


def preserved_archive(output):
    return {
        str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest() for p in output.rglob("*") if p.is_file()
    }


def rollback_case(image, prior_image, output):
    current = docker("run", "--detach", "--network", "none", "--memory", "512m", "--cpus", "1", image)
    prior = None
    try:
        active = ready(current)
        before = ownership(current, active["root"])
        archive = preserved_archive(output)
        prior = docker(
            "run",
            "--detach",
            "--network",
            "none",
            "--memory",
            "512m",
            "--cpus",
            "1",
            prior_image,
            "python",
            "/opt/fixture/compatibility_case.py",
            "server",
        )
        deadline = time.monotonic() + 20
        metadata = None
        while time.monotonic() < deadline:
            try:
                metadata = json.loads(
                    docker(
                        "exec",
                        prior,
                        "python",
                        "-c",
                        "from pathlib import Path; print(Path('/home/worker/current.json').read_text())",
                    )
                )
                break
            except subprocess.CalledProcessError:
                time.sleep(0.1)
        assert metadata is not None
        attempt = metadata["attempt"]
        prefix = [
            "exec",
            "--env",
            "HERDR_CONFIG_PATH=" + attempt + "/run/herdr.toml",
            "--env",
            "HOME=" + attempt + "/homes/codex",
            "--env",
            "XDG_RUNTIME_DIR=" + attempt + "/tmp",
            prior,
            "herdr",
        ]
        created = json.loads(docker(*prefix, "workspace", "create", "--cwd", attempt, "--no-focus"))
        pane = created["result"]["root_pane"]["pane_id"]
        docker(
            *prefix, "pane", "run", pane, shlex.join(["python", "/opt/fixture/compatibility_case.py", "agent", attempt])
        )
        while time.monotonic() < deadline:
            try:
                heartbeat = json.loads(
                    docker(
                        "exec",
                        prior,
                        "python",
                        "-c",
                        f"from pathlib import Path; print(Path({attempt!r},'run/compatibility-agent.json').read_text())",
                    )
                )
                break
            except subprocess.CalledProcessError:
                time.sleep(0.1)
        else:
            raise AssertionError("prior compatibility attempt did not start")
        after = ownership(current, active["root"])
        changed = sorted(
            k for k in before["hashes"] | after["hashes"] if before["hashes"].get(k) != after["hashes"].get(k)
        )
        assert before == after, changed
        assert archive == preserved_archive(output)
        assert (
            Path(attempt).name
            != json.loads(
                docker(
                    "exec",
                    current,
                    "python",
                    "-c",
                    "from pathlib import Path; print(Path('/home/worker/current.json').read_text())",
                )
            )["attempt"].split("/")[-1]
        )
        supervisor_present = (
            docker(
                "exec",
                prior,
                "python",
                "-c",
                "from pathlib import Path; print(Path('/opt/swarm-node/supervisor.py').exists())",
            )
            == "True"
        )
        return {
            "selected_prior_image_for_new_attempt": True,
            "new_attempt": Path(attempt).name,
            "actor": metadata["actor"],
            "compatibility_agent_pid": heartbeat["pid"],
            "active_ownership_before": before,
            "active_ownership_after": after,
            "existing_archive_preserved": archive == preserved_archive(output),
            "prior_image_supervisor_present": supervisor_present,
            "rollback_path": "retained worker headless herdr compatibility",
            "excluded_runtime_projections": [
                "herdr runtime journals and session projection",
                "herdr agent detection projection",
            ],
            "historical_supervisor_image": "not available in the retained prior worker image",
        }
    finally:
        if prior:
            docker("rm", "--force", prior)
        docker("kill", "--signal", "TERM", current)
        docker("wait", current, timeout=12)
        docker("rm", "--force", current)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--prior-image", required=True)
    parser.add_argument("--tested-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    inventory = json.loads(
        docker("run", "--rm", "--network", "none", args.image, "python", "/opt/swarm-node/worker_image.py", "report")
    )
    assert inventory["manifest"]["source_revision"] == args.tested_commit
    proof_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, text=True).strip()
    positive = [run_case(args.image, "complete", args.output) for _ in range(2)]
    negative = run_case(args.image, "complete", args.output, kill=True)
    recovery = [run_case(args.image, mode, args.output) for mode in ("late", "missing", "forced")]
    rollback = rollback_case(args.image, args.prior_image, args.output)
    fixture = Path(__file__).parent
    manifest = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (
            fixture / "process_fixture.py",
            fixture / "container_case.py",
            fixture / "compatibility_case.py",
            fixture / "Dockerfile",
        )
    }
    shared = {
        "package": "SV2-IMG-03",
        "tested_commit": args.tested_commit,
        "proof_commit": proof_commit,
        "supported_versions": inventory["manifest"]["observed"],
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
