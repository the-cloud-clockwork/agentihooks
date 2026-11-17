def tick_pass(store: RedisStore, ok: bool, now_ms: int, ports: Ports = port) -> list[str]:
    current = state(store)
    # ruleid: panic-sweep-spares-only-registered-fixes
    cancelled = cancel_runs(current.fixes, REPOS, ports)
    return [f"panic cancelled {line}" for line in cancelled]


def cmd_panic(store: RedisStore, args: argparse.Namespace) -> None:
    found = panic.engage(store, f"pressed on swarm {args.slug}", False, args.fix, now_ms())
    # ruleid: panic-sweep-spares-only-registered-fixes
    cancelled = panic.cancel_runs(found.fixes, panic.REPOS, panic.port)
    print(json.dumps({"panic": "on", "fixes": list(found.fixes), "cancelled": cancelled}))


def tick_pass_sparing_fixers(store: RedisStore, ok: bool, now_ms: int, ports: Ports = port) -> list[str]:
    current = state(store)
    # ok: panic-sweep-spares-only-registered-fixes
    cancelled = cancel_runs((*current.fixes, *fixers(store)), REPOS, ports)
    return [f"panic cancelled {line}" for line in cancelled]


def cmd_panic_sparing_fixers(store: RedisStore, args: argparse.Namespace) -> None:
    found = panic.engage(store, f"pressed on swarm {args.slug}", False, args.fix, now_ms())
    # ok: panic-sweep-spares-only-registered-fixes
    cancelled = panic.cancel_runs((*found.fixes, *panic.fixers(store)), panic.REPOS, panic.port)
    print(json.dumps({"panic": "on", "fixes": list(found.fixes), "cancelled": cancelled}))
