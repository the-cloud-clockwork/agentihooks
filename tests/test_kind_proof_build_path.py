from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh").read_text()


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
