def tick_pass(store: RedisStore, ok: bool, now_ms: int, ports: Ports = port) -> list[str]:
    count_pass(store, ok, now_ms)
    actions = [changed] if (changed := alarm(store, now_ms)) else []
    current = state(store)
    if current is None or not due_cancel(store, now_ms):
        return actions
    try:
        # ruleid: panic-sweep-on-pass-thread
        cancelled = cancel_runs(current.fixes, REPOS, ports, store)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, SwarmError) as error:
        return [*actions, f"panic cancel failed: {error}"]
    return [*actions, *(f"panic cancelled {line}" for line in cancelled)]


def tick_pass(
    store: RedisStore, ok: bool, now_ms: int, report: Callable[[str], None], ports: Ports = port
) -> list[str]:
    current = state(store)
    if current is None or not due_cancel(store, now_ms):
        return []
    # ruleid: panic-sweep-on-pass-thread
    for line in _swept(current.fixes, ports, store):
        report(line)
    return []


def tick_pass(
    store: RedisStore, ok: bool, now_ms: int, report: Callable[[str], None], ports: Ports = port
) -> list[str]:
    current = state(store)
    if current is None or not due_cancel(store, now_ms) or not SWEEP.acquire(blocking=False):
        return []
    # ruleid: panic-sweep-on-pass-thread
    _sweep(current.fixes, ports, store, report)
    return []


def tick_pass(
    store: RedisStore, ok: bool, now_ms: int, report: Callable[[str], None], ports: Ports = port
) -> list[str]:
    count_pass(store, ok, now_ms)
    actions = [changed] if (changed := alarm(store, now_ms)) else []
    current = state(store)
    if current is None or not due_cancel(store, now_ms) or not SWEEP.acquire(blocking=False):
        return actions
    # ok: panic-sweep-on-pass-thread
    Thread(target=_sweep, args=(current.fixes, ports, store, report), name="panic-sweep", daemon=True).start()
    return actions


def _swept(fixes: Iterable[str], ports: Ports, store: RedisStore) -> list[str]:
    try:
        # ok: panic-sweep-on-pass-thread
        return [f"panic cancelled {line}" for line in cancel_runs(fixes, REPOS, ports, store)]
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, SwarmError) as error:
        return [f"panic cancel failed: {error}"]


def cmd_panic(store: RedisStore, args: argparse.Namespace) -> None:
    found = panic.engage(store, "pressed", False, args.fix, now)
    # ok: panic-sweep-on-pass-thread
    cancelled = panic.cancel_runs(found.fixes, panic.REPOS, panic.port)
    print(cancelled)
