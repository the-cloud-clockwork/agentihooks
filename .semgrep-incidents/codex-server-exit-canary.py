def silent_exit(pump: object, server: object, spec: object) -> None:
    while pump.poll() is None:
        try:
            # ruleid: codex-server-exit-canary
            server.wait(timeout=POLL_S)
            break
        except TimeoutExpired:
            pass


def failed_canary(pump: object, server: object, spec: object) -> None:
    while pump.poll() is None:
        try:
            # ok: codex-server-exit-canary
            _server_exited(spec, server.wait(timeout=POLL_S))
            break
        except TimeoutExpired:
            pass
