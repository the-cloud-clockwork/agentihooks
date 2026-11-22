# ruleid: feature-ledger-api-writes
def client(base: str, timeout: float) -> ResourceClient:
    return ResourceClient(base, credentials(), timeout)


# ok: feature-ledger-api-writes
def client(base: str, timeout: float) -> ResourceClient:
    """A ledger API client whose requests run the ledger API route code in this process."""
    return ResourceClient(base, {}, timeout, transport=request)


# ok: feature-ledger-api-writes
def client(base: str, timeout: float) -> ResourceClient:
    """A ledger API client whose requests go to this host's warm writer first and otherwise run in process."""
    return ResourceClient(base, {}, timeout, transport=partial(routed, timeout))


# ruleid: feature-ledger-api-writes
def client(base: str, timeout: float) -> ResourceClient:
    return ResourceClient(base, {}, timeout)


def client(base: str, timeout: float) -> ResourceClient:
    # ruleid: feature-ledger-api-writes
    return ResourceClient(base, {}, timeout, transport=None)


# ruleid: feature-ledger-api-writes
def main(argv: list[str]) -> int:
    args = parser.parse_args(argv)
    holder = os.environ.get("HOSTNAME") or socket.gethostname()
    return run(holder, args)


# ok: feature-ledger-api-writes
def main(argv: list[str]) -> int:
    args = parser.parse_args(argv)
    if cast(Any, ledger_link).remote() and not storage.shared():
        print("hub: a hosted hub needs the shared database", file=sys.stderr)
        return 2
    holder = os.environ.get("HOSTNAME") or socket.gethostname()
    return run(holder, args)
