import functools
import os
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("stall", [False, True], ids=["normal", "stalled-once"])
def test_real_protected_checkout_recovers_within_budget(tmp_path, stall):
    action = Path(os.environ["RUNNER_WORKSPACE"]).parent / "_actions/actions/checkout/v4/dist/index.js"
    assert action.is_file(), action
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    checkout = workflow["jobs"]["select"]["steps"][0]
    remote = tmp_path / "server/owner/repo"
    remote.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    source = tmp_path / "source"
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Proof",
            "-c",
            "user.email=proof@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "Protected tree",
        ],
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(
        ["git", "-C", str(source), "push", str(remote), "HEAD:refs/heads/protected"], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(remote), "update-server-info"], check=True, capture_output=True)
    release = threading.Event()
    requests = []

    class Remote(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/owner/repo/info/refs"):
                requests.append(time.monotonic())
                if stall and len(requests) == 1:
                    release.wait(110)
                    self.close_connection = True
                    return
            super().do_GET()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Remote, directory=remote.parents[1]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_REF": "refs/heads/protected",
        "GITHUB_SHA": sha,
        "RUNNER_TEMP": str(tmp_path),
        "INPUT_REPOSITORY": "owner/repo",
        "INPUT_REF": sha,
        "INPUT_PATH": checkout["with"]["path"],
        "INPUT_FETCH-DEPTH": str(checkout["with"]["fetch-depth"]),
        "INPUT_PERSIST-CREDENTIALS": str(checkout["with"]["persist-credentials"]).lower(),
        "INPUT_GITHUB-SERVER-URL": f"http://127.0.0.1:{server.server_port}",
        "INPUT_TOKEN": "local-fixture",
        **checkout.get("env", {}),
    }
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["node", str(action)], cwd=workspace, env=env, capture_output=True, text=True, timeout=100
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(requests) == (2 if stall else 1), result.stdout
    assert elapsed < (95 if stall else 10), elapsed
    if stall:
        assert 55 <= requests[1] - requests[0] <= 85
        assert "Operation too slow" in result.stdout + result.stderr
        assert "seconds before trying again" in result.stdout
    checked = workspace / checkout["with"]["path"]
    assert subprocess.check_output(["git", "-C", str(checked), "rev-parse", "HEAD"], text=True).strip() == sha
    assert not (checked / ".git/shallow").exists()
    config = subprocess.check_output(["git", "-C", str(checked), "config", "--local", "--list"], text=True)
    assert "extraheader" not in config.lower()
    print(f"protected checkout stall={stall} fetch_attempts={len(requests)} elapsed_seconds={elapsed:.3f}")
