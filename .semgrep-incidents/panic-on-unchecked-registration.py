def cmd_panic(store: RedisStore, args: argparse.Namespace) -> None:
    now = now_ms()
    if args.action == "on":
        # ruleid: panic-on-unchecked-registration
        found = panic.engage(store, f"pressed on swarm {args.slug}", False, args.fix, now)
        print(found)


def cmd_panic(store: RedisStore, args: argparse.Namespace) -> None:
    now = now_ms()
    if args.action == "on":
        _master_or_operator(store, args, "only the master or the operator presses panic on")
        # ruleid: panic-on-unchecked-registration
        found = panic.engage(store, f"pressed on swarm {args.slug}", False, args.fix, now)
        print(found)


def cmd_panic(store: RedisStore, args: argparse.Namespace) -> None:
    now = now_ms()
    if args.action == "on":
        for fix in args.fix:
            panic.check_fix(fix)
        # ruleid: panic-on-unchecked-registration
        found = panic.engage(store, f"pressed on swarm {args.slug}", False, args.fix, now)
        print(found)


def cmd_panic(store: RedisStore, args: argparse.Namespace) -> None:
    now = now_ms()
    if args.action == "on":
        # ruleid: panic-on-unchecked-registration
        found = panic.engage(store, f"pressed on swarm {args.slug}", False, args.fix, now)
        _master_or_operator(store, args, "only the master or the operator presses panic on")
        for fix in args.fix:
            panic.check_fix(fix)
        print(found)


def press(store: RedisStore, fixes: list[str]) -> None:
    # ruleid: panic-on-unchecked-registration
    panic.engage(store, "pressed by a helper", False, fixes, 0)


def cmd_panic(store: RedisStore, args: argparse.Namespace) -> None:
    now = now_ms()
    if args.action == "on":
        _master_or_operator(store, args, "only the master or the operator presses panic on")
        for fix in args.fix:
            panic.check_fix(fix)
        # ok: panic-on-unchecked-registration
        found = panic.engage(store, f"pressed on swarm {args.slug}", False, args.fix, now)
        print(found)
