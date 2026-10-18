import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh").read_text()


def _at(text: str) -> int:
    assert text in SCRIPT, text
    return SCRIPT.index(text)


def test_chart_install_waits_only_for_the_images_it_runs():
    job = _at('wait "$build"\n(\n')
    started = _at("terminal=$!\n")
    background = SCRIPT[job:started]
    assert 'bash scripts/packaging/swarm-image-clients.sh "$image"' in background
    assert '-t "$terminal_image"' in background
    assert 'flock "$load_lock" kind load docker-image "$terminal_image" --name "$cluster"' in background
    main_load = _at('flock "$load_lock" kind load docker-image "$image" "$bundle_image" --name "$cluster"')
    install = _at('helm install "$release"')
    terminal_wait = _at('wait "$terminal"\nbash "$chart/ci/terminal-attach-proof.sh" "$terminal_image"')
    assert job < started < main_load < install < terminal_wait
    assert SCRIPT.count('wait "$terminal"\n') == 1


def test_kind_proof_cleanup_waits_for_the_terminal_image_job():
    start = _at("finish() {")
    finish = SCRIPT[start : SCRIPT.index("\n}\n", start)]
    assert finish.index('wait "$terminal" || true') < finish.index('rm -f "$load_lock"')
    assert SCRIPT.count('load_lock="$(mktemp)"\n') == 1
    assert 'load_lock="$(mktemp)"\ntrap finish EXIT\n' in SCRIPT


@pytest.fixture
def cache(tmp_path):
    folder = tmp_path / "kind-layers"
    folder.mkdir()
    return folder


def _layer_flags(cache, env, present=(), then="keep_layers swarm"):
    start = _at("layers() {")
    functions = SCRIPT[start : SCRIPT.index("\n}\n", SCRIPT.index("keep_layers() {")) + 3]
    for name in present:
        (cache / name).mkdir()
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{functions}\nlayers swarm\n{then}\nls {cache}"],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


@pytest.mark.parametrize(
    ("hit", "present", "flags"),
    [
        ("true", ("swarm",), ["--cache-from", "type=local,src={cache}/swarm"]),
        (
            "false",
            ("swarm",),
            [
                "--cache-from",
                "type=local,src={cache}/swarm",
                "--cache-to",
                "type=local,dest={cache}/swarm.next,mode=max",
            ],
        ),
        ("", (), ["--cache-to", "type=local,dest={cache}/swarm.next,mode=max"]),
    ],
    ids=["hit-reads-only", "miss-reads-and-writes", "cold-writes-only"],
)
def test_fixture_builds_read_the_restored_layers_and_write_only_on_a_miss(cache, hit, present, flags):
    env = {"KIND_LAYER_CACHE": str(cache), "KIND_LAYER_CACHE_HIT": hit}
    expected = [flag.format(cache=cache) for flag in flags] + ["complete", *present]
    assert _layer_flags(cache, env, present) == expected


def test_a_new_layer_export_replaces_the_restored_one(cache):
    (cache / "swarm").mkdir()
    (cache / "swarm" / "stale").touch()
    (cache / "swarm.next").mkdir()
    _layer_flags(cache, {"KIND_LAYER_CACHE": str(cache), "KIND_LAYER_CACHE_HIT": "false"})
    assert sorted(path.name for path in cache.iterdir()) == ["complete", "swarm"]
    assert not (cache / "swarm" / "stale").exists()


def test_a_restored_completion_mark_is_cleared_before_any_step_can_fail():
    clear = _at(
        'if [[ -n ${KIND_LAYER_CACHE:-} ]]; then\n  mkdir -p "$KIND_LAYER_CACHE"\n  rm -f "$KIND_LAYER_CACHE/complete"\nfi\n'
    )
    assert (
        clear < _at("python3 tests/kind_request.py") < _at("pull-runtime-image.sh") < _at("keep_layers swarm terminal")
    )
    assert SCRIPT.count('rm -f "$KIND_LAYER_CACHE/complete"') == 1


def test_fixture_builds_without_a_layer_cache_pass_no_cache_flags(cache):
    assert _layer_flags(cache, {}) == []


def test_every_fixture_build_stage_takes_the_layer_flags():
    assert 'docker build -q "${terminal_layers[@]}" --build-arg IMAGE="$image" --target packages' in SCRIPT
    assert 'docker build -q -t "$image" "${swarm_layers[@]}" .' in SCRIPT
    assert _at('wait "$packages"\n  keep_layers swarm terminal\n') < _at("build=$!\n")


def test_the_kind_job_restores_and_saves_the_layers_it_builds_with():
    workflow = yaml.safe_load((ROOT / ".github/workflows/helm-kind.yml").read_text())
    steps = {step.get("id") or step.get("name"): step for step in workflow["jobs"]["kind"]["steps"]}
    smoke_name = "Lint, install and prove the chart in kind"
    restore, smoke, save = steps["layers"], steps[smoke_name], steps["Save the fixture image layers"]
    assert restore["uses"] == "actions/cache/restore@v4"
    assert save["uses"] == "actions/cache/save@v4"
    assert restore["with"]["path"] == save["with"]["path"] == smoke["env"]["KIND_LAYER_CACHE"]
    assert smoke["env"]["KIND_LAYER_CACHE_HIT"] == "${{ steps.layers.outputs.cache-hit }}"
    assert save["with"]["key"] == "${{ steps.layers.outputs.cache-primary-key }}"
    exported = steps["exported"]
    assert exported["if"] == "always() && steps.key.outputs.hit != 'true' && steps.layers.outputs.cache-hit != 'true'"
    assert "$RUNNER_TEMP/kind-layers/complete" in exported["run"]
    assert restore["with"]["path"] == "${{ runner.temp }}/kind-layers"
    assert save["if"] == "always() && steps.key.outputs.hit != 'true' && steps.exported.outputs.complete == 'true'"
    order = list(steps)
    assert order.index("layers") < order.index(smoke_name) < order.index("exported")
    assert order.index("exported") < order.index("Save the fixture image layers")
    sources = (
        "Dockerfile",
        "pyproject.toml",
        "README.md",
        "docker/swarm/requirements.lock",
        "docker/swarm-node/versions.lock",
        "docker/swarm-node/requirements.lock",
        "scripts/swarm_v2/worker_image.py",
        "deploy/helm/agentihooks-swarm/ci/terminal-worker.Dockerfile",
    )
    assert restore["with"]["key"] == f"kind-layers-${{{{ hashFiles({', '.join(map(repr, sources))}) }}}}"


def test_requirements_layer_depends_on_the_lock_alone():
    stage = (ROOT / "Dockerfile").read_text()
    stage = stage[: stage.index("COPY hooks/ hooks/")]
    assert any(
        command in stage
        for command in (
            "COPY docker/swarm/requirements.lock ./\nRUN python -m venv /opt/venv",
            "COPY docker/swarm/requirements.lock ./\nRUN --mount=type=cache,target=/root/.cache/uv \\\n"
            "    uv venv /opt/venv && uv pip install --python /opt/venv/bin/python "
            "--compile-bytecode --require-hashes -r requirements.lock",
        )
    )
    assert stage.index("-r requirements.lock") < stage.index("COPY pyproject.toml README.md ./")


def test_controller_setup_runs_beside_the_lease_proof_and_finishes_before_the_upgrade():
    start = _at('ca_dir="$(mktemp -d)"\n(\n')
    started = _at("prep=$!\n")
    background = SCRIPT[start:started]
    for step in (
        "kubectl create secret generic swarm-launch-signing",
        "kubectl create serviceaccount swarm-controller",
        "kubectl create configmap swarm-pod-policy",
        "kubectl create secret generic swarm-terminal-attach-ca",
    ):
        assert step in background
        assert SCRIPT.count(step) == 1
    order = [
        'helm test "$release"',
        'ca_dir="$(mktemp -d)"\n(\n',
        "prep=$!\n",
        "registered, no lease bound",
        "pkill -STOP",
        'if ! wait "$prep"; then',
        'fi\nhelm upgrade "$release"',
    ]
    assert [_at(text) for text in order] == sorted(_at(text) for text in order)


def test_one_exec_waits_for_the_lease_and_its_renewal():
    watch = SCRIPT[_at("python - <<'LEASE'\nimport dataclasses") : _at("pkill -STOP")]
    assert "controller lease: " in watch
    assert "controller lease renewed: " in watch
    assert 'held["expires_at"] > first["expires_at"]' in watch
    assert "sleep 1" not in watch
    assert "current = lease.current(connect(), slug)\n        except Exception:\n            current = None" in watch


def test_every_controller_probe_still_runs_and_fails_the_proof():
    probes = SCRIPT[_at("declare -A probe_jobs") : _at('kubectl wait pod "${controller[@]}" --for=condition=Ready')]
    assert probes.count("for probe in startup liveness readiness; do") == 3
    reaped = probes.index('wait "${probe_jobs[$probe]}" || probe_status[$probe]=$?')
    failed = probes.index("if [[ ${probe_status[$probe]} != 0 ]]; then")
    assert reaped < failed < probes.index("probe failed: %s\\n", failed) < probes.index("exit 1", failed)
