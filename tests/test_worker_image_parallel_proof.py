import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKER = """import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
state = Path(os.environ['STATE'])
with (state / 'calls').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
def wait(name):
    deadline = time.monotonic() + 3
    while not (state / name).exists():
        if time.monotonic() >= deadline:
            sys.exit(41)
        time.sleep(0.01)
if args[0] == 'build':
    if '--no-cache-filter' in args:
        wait('scan-started')
        wait('profile-started')
        (state / 'rebuild').touch()
    lock = json.loads((Path(args[-1]) / 'docker/swarm-node/versions.lock').read_text())
    tool = lock['tools']['herdr']
    if 'sha256' not in tool:
        print('invalid artifact lock: herdr')
        sys.exit(1)
    if lock['architectures'] == ['arm64']:
        print('unsupported architecture')
        sys.exit(1)
    if tool['sha256'] == '0' * 64:
        print('artifact checksum mismatch: herdr')
        sys.exit(1)
elif args[:2] == ['image', 'inspect']:
    sys.exit(1)
elif args[0] == 'info':
    print(os.environ.get('DRIVER', '[]'))
elif args[0] == 'run':
    if '/opt/probe/profile-probe.sh' in args:
        if args[-1] == 'positive':
            (state / 'profile-started').touch()
            wait('rebuild')
        if os.environ['FAILURE'] == 'profile':
            sys.exit(18)
        print('{}')
    else:
        print(json.dumps({'manifest': {'source_revision': 'checked-revision',
              'declared': {'lock_sha256': 'locked'}},
              'worker_image_build_validation_failures': 0}))
"""
SCAN = """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == --pull-scanner ]]; then
    if [[ "$FAILURE" == prefetch ]]; then exit 21; fi
    touch "$STATE/scanner-pulled"
    exit 0
fi
if [[ "$1" == agentihooks-worker-proof:* ]]; then
    touch "$STATE/scan-started"
    for ((attempt=0; attempt<300; attempt++)); do
        if [[ -e "$STATE/rebuild" ]]; then break; fi
        sleep 0.01
    done
    [[ -e "$STATE/rebuild" ]] || exit 41
    if [[ "$FAILURE" == scan ]]; then exit 17; fi
fi
touch "$STATE/scan-${1%%:*}-finished"
"""
PROFILE = """import os, sys
from pathlib import Path
state = Path(os.environ['STATE'])
(state / 'profile-evidence').touch()
if os.environ['FAILURE'] == 'profile-evidence':
    sys.exit(19)
"""


def run_proof(tmp_path, failure, proof="all", driver="[]"):
    state = tmp_path / "state"
    state.mkdir()
    if proof != "all":
        for name in ("scan-started", "profile-started", "rebuild"):
            (state / name).touch()
    commands = tmp_path / "bin"
    commands.mkdir()
    archive = tmp_path / "source.tar"
    scan = (
        SCAN
        if proof == "all"
        else (
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            'if [[ "$1" == --pull-scanner ]]; then\n'
            '    if [[ "$FAILURE" == prefetch ]]; then exit 21; fi\n'
            '    touch "$STATE/scanner-pulled"; exit 0\n'
            "fi\n"
            'touch "$STATE/scan-${1%%:*}-finished"\n'
            'if [[ "$FAILURE" == scan ]]; then exit 17; fi\n'
        )
    )
    files = {
        "docker/swarm-node/versions.lock": json.dumps(
            {"tools": {"herdr": {"sha256": "1" * 64}}, "architectures": ["amd64"]}
        ),
        "docker/swarm-node/scan.sh": scan,
        "docker/swarm-node/Dockerfile": "FROM scratch\n",
        "docker/swarm-node/fixtures/profiles/profile.json": "{}",
        "tests/swarm_node/profile_evidence.py": PROFILE,
        "tests/contracts/swarm_v2/fixtures/checkpoint/current.json": "{}",
    }
    with tarfile.open(archive, "w") as bundle:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            payload = content.encode()
            entry.size = len(payload)
            bundle.addfile(entry, io.BytesIO(payload))
    scripts = {
        "docker": f"#!{sys.executable}\n" + DOCKER,
        "git": "#!/usr/bin/env bash\nset -euo pipefail\n"
        'case "$*" in\n'
        '    "rev-parse --show-toplevel") printf "%s\\n" "$REPO" ;;\n'
        '    "rev-parse HEAD") echo checked-revision ;;\n'
        '    "-C "*" archive HEAD") cat "$ARCHIVE" ;;\n'
        "    *) exit 1 ;;\nesac\n",
    }
    for name, content in scripts.items():
        path = commands / name
        path.write_text(content)
        path.chmod(0o755)
    output = tmp_path / "evidence"
    env = {key: value for key, value in os.environ.items() if key != "WORKER_BASE_CONTEXT"} | {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "STATE": str(state),
        "REPO": str(tmp_path),
        "ARCHIVE": str(archive),
        "FAILURE": failure,
        "DRIVER": driver,
    }
    result = subprocess.run(
        ["bash", str(ROOT / "docker/swarm-node/smoke.sh"), str(output), proof],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    calls = [json.loads(line) for line in (state / "calls").read_text().splitlines()]
    return result, calls, state, output


def test_independent_worker_proofs_overlap_and_keep_every_check(tmp_path):
    result, calls, state, output = run_proof(tmp_path, "")
    assert result.returncode == 0, result.stderr
    assert "SV2-IMG-01 passed" in result.stdout
    assert (state / "profile-evidence").exists()
    assert (state / "scan-agentihooks-worker-proof-finished").exists()
    assert (state / "scan-agentihooks-worker-rebuild-finished").exists()
    builds = [call for call in calls if call[0] == "build"]
    assert len(builds) == 5
    assert "--no-cache-filter" in builds[-1]
    assert builds[-1][builds[-1].index("--no-cache-filter") + 1] == "worker"
    runs = [call for call in calls if call[0] == "run"]
    assert len(runs) == 12
    assert all("--read-only" in call and call[call.index("--network") + 1] == "none" for call in runs)
    profiles = [call[-1] for call in runs if "/opt/probe/profile-probe.sh" in call]
    assert profiles == ["positive", "positive", "rejection", "recovery", "rollback", "noexec"]
    assert json.loads((output / "result.json").read_text())["status"] == "passed"
    assert not list(output.glob("context-*"))
    assert calls[-1][:2] == ["image", "rm"]


@pytest.mark.parametrize("failure,status", [("scan", 17), ("profile", 18), ("profile-evidence", 19)])
def test_a_failed_background_worker_check_keeps_the_proof_red(tmp_path, failure, status):
    result, calls, state, output = run_proof(tmp_path, failure)
    assert result.returncode == status, result.stderr
    assert (state / "rebuild").exists()
    assert "SV2-IMG-01 passed" not in result.stdout
    assert not (output / "result.json").exists()
    assert not list(output.glob("context-*"))
    assert calls[-1][:2] == ["image", "rm"]


@pytest.mark.parametrize("proof,builds,runs", [("startup", 4, 5), ("rebuild", 1, 1), ("profiles", 1, 7)])
def test_worker_proof_modes_run_only_their_independent_checks(tmp_path, proof, builds, runs):
    result, calls, state, output = run_proof(tmp_path, "", proof)
    assert result.returncode == 0, result.stderr
    assert len([call for call in calls if call[0] == "build"]) == builds
    assert len([call for call in calls if call[0] == "run"]) == runs
    assert json.loads((output / "mode-result.json").read_text())["proof"] == proof
    assert not list(output.glob("context-*"))
    if proof == "rebuild":
        build = next(call for call in calls if call[0] == "build")
        assert build[build.index("--no-cache-filter") + 1] == "worker"
    if proof == "profiles":
        assert (state / "profile-evidence").exists()
    assert (state / "scanner-pulled").exists()
    assert (state / "scan-agentihooks-worker-proof-finished").exists()


@pytest.mark.parametrize("proof", ["startup", "rebuild", "profiles"])
@pytest.mark.parametrize(
    "driver,exported", [("[[driver-type io.containerd.snapshotter.v1]]", True), ("[[Backing Filesystem extfs]]", False)]
)
def test_containerd_store_exports_worker_layers_uncompressed(tmp_path, proof, driver, exported):
    result, calls, state, output = run_proof(tmp_path, "", proof, driver)
    assert result.returncode == 0, result.stderr
    build = next(call for call in calls if call[0] == "build")
    output_flag = "--output" in build and build[build.index("--output") + 1] == "type=image,compression=uncompressed"
    assert output_flag is exported
    assert ("--provenance=false" in build) is exported
    assert build[build.index("-t") + 1].startswith("agentihooks-worker-proof:")
    assert (output / "docker-driver.txt").read_text().strip() == driver


@pytest.mark.parametrize(
    "proof,failure,status",
    [
        ("startup", "scan", 17),
        ("rebuild", "scan", 17),
        ("startup", "prefetch", 21),
        ("rebuild", "prefetch", 21),
        ("profiles", "prefetch", 21),
        ("profiles", "profile", 18),
        ("profiles", "profile-evidence", 19),
    ],
)
def test_a_failed_selected_worker_proof_never_reports_success(tmp_path, proof, failure, status):
    result, calls, state, output = run_proof(tmp_path, failure, proof)
    assert result.returncode == status, result.stderr
    assert not (output / "mode-result.json").exists()
    assert not list(output.glob("context-*"))


@pytest.mark.parametrize(
    "change", ["none", "inventory", "rollback", "profiles", "revision", "status", "missing", "profile"]
)
def test_parallel_worker_reports_require_equal_inventories_and_all_proofs(tmp_path, change):
    evidence = tmp_path / "evidence"
    report = {
        "manifest": {"source_revision": "checked-revision", "declared": {"lock_sha256": "locked"}},
        "worker_image_build_validation_failures": 0,
    }
    different = report | {"different": True}
    for proof in ("startup", "rebuild", "profiles"):
        folder = evidence / proof
        folder.mkdir(parents=True)
        (folder / "mode-result.json").write_text(
            json.dumps({"proof": proof, "tested_commit": "checked-revision", "status": "passed", "fixture": proof})
        )
    for name in ("first", "second"):
        (evidence / "startup" / f"{name}.json").write_text(json.dumps(report))
    (evidence / "startup" / "rollback.json").write_text(json.dumps(different if change == "rollback" else report))
    (evidence / "profiles" / "profiles.json").write_text(json.dumps(different if change == "profiles" else report))
    for case in ("a", "b"):
        (evidence / "startup" / f"{case}-result.json").write_text(
            json.dumps({"package": "SV2-IMG-01", "fixture": "startup-fixture", "tested_commit": "checked-revision"})
        )
    (evidence / "rebuild" / "rebuild.json").write_text(json.dumps(different if change == "inventory" else report))
    profiles = evidence / "profiles" / "SV2-IMG-02"
    profiles.mkdir()
    (profiles / "result.json").write_text(json.dumps({"status": "passed", "tested_commit": "checked-revision"}))
    if change == "revision":
        (evidence / "rebuild" / "mode-result.json").write_text(
            json.dumps({"proof": "rebuild", "tested_commit": "other", "status": "passed", "fixture": "rebuild"})
        )
    if change == "status":
        (evidence / "startup" / "mode-result.json").write_text(
            json.dumps(
                {"proof": "startup", "tested_commit": "checked-revision", "status": "failed", "fixture": "startup"}
            )
        )
    if change == "missing":
        (evidence / "rebuild" / "rebuild.json").unlink()
    if change == "profile":
        (profiles / "result.json").write_text(json.dumps({"status": "failed", "tested_commit": "checked-revision"}))
    done = subprocess.run(
        ["bash", str(ROOT / "docker/swarm-node/smoke.sh"), str(evidence), "compare", "checked-revision"],
        capture_output=True,
        text=True,
    )
    assert (done.returncode == 0) == (change == "none"), done.stderr
    assert (evidence / "result.json").exists() == (change == "none")
    if change in ("none", "inventory", "rollback", "profiles"):
        recovery = json.loads((evidence / "c-result.json").read_text())
        assert recovery["fixture"] == "startup"
        assert recovery["leg_fixtures"] == {"startup": "startup", "rebuild": "rebuild", "profiles": "profiles"}
        assert recovery["lock_sha256"] == "locked"
        assert recovery["uncached_rebuild_inventory_equal"] is (change != "inventory")
        assert recovery["retained_image_started_after_rejected_builds"] is (change != "rollback")
        assert "retained_previous_image_started" not in recovery


HEALTH_PYTHON = f"""#!{sys.executable}
import os, subprocess, sys, time
from pathlib import Path
if sys.argv[1] == '-c':
    sys.exit(subprocess.run([{sys.executable!r}, *sys.argv[1:]]).returncode)
state = Path(os.environ['STATE'])
name = Path(sys.argv[1]).stem
other = 'entrypoint_proof' if name == 'health_proof' else 'health_proof'
(state / f'{{name}}-started').touch()
deadline = time.monotonic() + 3
while not (state / f'{{other}}-started').exists():
    if time.monotonic() >= deadline:
        sys.exit(41)
    time.sleep(0.01)
print(f'{{name}} ran')
sys.exit(int(os.environ.get(name.upper(), '0')))
"""


@pytest.mark.parametrize("failure", ["", "HEALTH_PROOF", "ENTRYPOINT_PROOF"])
def test_health_and_entrypoint_proofs_overlap_and_either_failure_stays_red(tmp_path, failure):
    state = tmp_path / "state"
    state.mkdir()
    commands = tmp_path / "bin"
    commands.mkdir()
    (commands / "docker").write_text('#!/usr/bin/env bash\nif [[ "${!#}" == - ]]; then cat > /dev/null; fi\n')
    (commands / "python").write_text(HEALTH_PYTHON)
    for name in ("docker", "python"):
        (commands / name).chmod(0o755)
    env = (
        {key: value for key, value in os.environ.items() if key != "WORKER_BASE_CONTEXT"}
        | {
            "PATH": f"{commands}:{os.environ['PATH']}",
            "STATE": str(state),
            "SWARM_PROOF_PYTHON": str(commands / "python"),
        }
        | ({failure: "23"} if failure else {})
    )
    done = subprocess.run(
        ["bash", str(ROOT / "tests/integration/swarm_node/run_health_proof.sh"), str(tmp_path / "out")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
    )
    assert done.returncode == (23 if failure else 0), done.stderr
    assert "health_proof ran" in done.stdout
    assert "entrypoint_proof ran" in done.stdout


@pytest.mark.parametrize(
    "driver,exported", [("[[driver-type io.containerd.snapshotter.v1]]", True), ("[[Backing Filesystem extfs]]", False)]
)
def test_health_worker_build_exports_uncompressed_on_the_containerd_store(tmp_path, driver, exported):
    state = tmp_path / "state"
    state.mkdir()
    commands = tmp_path / "bin"
    commands.mkdir()
    (commands / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == info ]]; then echo "$DRIVER"; exit 0; fi\n'
        'printf "%s\\n" "$*" >> "$STATE/docker-calls"\n'
        'if [[ "${!#}" == - ]]; then cat > /dev/null; fi\n'
    )
    (commands / "python").write_text(HEALTH_PYTHON)
    for name in ("docker", "python"):
        (commands / name).chmod(0o755)
    env = {key: value for key, value in os.environ.items() if key != "WORKER_BASE_CONTEXT"} | {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "STATE": str(state),
        "DRIVER": driver,
        "SWARM_PROOF_PYTHON": str(commands / "python"),
    }
    done = subprocess.run(
        ["bash", str(ROOT / "tests/integration/swarm_node/run_health_proof.sh"), str(tmp_path / "out")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
    )
    assert done.returncode == 0, done.stderr
    builds = [line for line in (state / "docker-calls").read_text().splitlines() if line.startswith("build ")]
    worker = next(line for line in builds if "docker/swarm-node/Dockerfile" in line)
    assert ("--provenance=false --output type=image,compression=uncompressed" in worker) is exported
    assert not any("--output" in line for line in builds if line != worker)
    assert (tmp_path / "out" / "docker-driver.txt").read_text().strip() == driver
