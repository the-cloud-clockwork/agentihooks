from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh").read_text()


def _at(text: str) -> int:
    assert text in SCRIPT, text
    return SCRIPT.index(text)


def test_chart_install_waits_only_for_the_images_it_runs():
    build = _at('wait "$build"\n')
    terminal_build = _at('-t "$terminal_image"')
    terminal_load = _at('flock "$load_lock" kind load docker-image "$terminal_image" --name "$cluster"')
    clients = _at('bash scripts/packaging/swarm-image-clients.sh "$image"')
    main_load = _at('flock "$load_lock" kind load docker-image "$image" "$bundle_image" --name "$cluster"')
    install = _at('helm install "$release"')
    terminal_wait = _at('wait "$terminal"\nbash "$chart/ci/terminal-attach-proof.sh" "$terminal_image"')
    assert build < terminal_build < terminal_load < clients < main_load < install < terminal_wait


def test_kind_proof_cleanup_waits_for_the_terminal_image_job():
    start = _at("finish() {")
    finish = SCRIPT[start : SCRIPT.index("\n}\n", start)]
    assert 'wait "$terminal" || true' in finish
    assert 'rm -f "$load_lock"' in finish
