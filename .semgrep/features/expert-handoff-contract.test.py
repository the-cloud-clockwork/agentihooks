def cmd_handoff(store, args):
    agent = _me(store, args)
    text = _read(args.doc, "handoff document")
    # ruleid: feature-expert-handoff-contract
    found = handoff_check.problems(text, Resolver(args.slug, store.redis, ledger.item))
    if found:
        raise SwarmError(handoff_check.refusal(found))


def cmd_handoff(store, args):
    agent = _me(store, args)
    text = _read(args.doc, "handoff document")
    # ok: feature-expert-handoff-contract
    found = handoff_contracts.problems(store, args.slug, agent, text, Resolver(args.slug, store.redis, ledger.item))
    if found:
        raise SwarmError(handoff_check.refusal(found))


def cmd_ready(store, args):
    # ok: feature-expert-handoff-contract
    problems = handoff_check.problems(text, Resolver(args.slug, store.redis, ledger.item))
    return problems


def _handoff_refusal(command, slug):
    text = Path(tokens[4]).expanduser().read_text(encoding="utf-8")
    # ruleid: feature-expert-handoff-contract
    found = handoff_check.problems(text, _resolver(slug))
    return "BLOCKED: " + handoff_check.refusal(found) if found else None


def _handoff_refusal(command, slug, environ):
    text = Path(tokens[4]).expanduser().read_text(encoding="utf-8")
    # ok: feature-expert-handoff-contract
    found = _problems(text, slug, environ)
    return "BLOCKED: " + handoff_check.refusal(found) if found else None


def _problems(text, slug, environ):
    resolves = _resolver(slug)
    if not _expert(environ):
        # ok: feature-expert-handoff-contract
        return handoff_check.problems(text, resolves)
    store = connect()
    return contracts.problems(store, slug, agent, text, resolves)


def _problems(text, slug, environ):
    resolves = _resolver(slug)
    if _expert(environ):
        # ruleid: feature-expert-handoff-contract
        return handoff_check.problems(text, resolves)
    return contracts.problems(connect(), slug, agent, text, resolves)


def _problems(text, slug, environ):
    # ruleid: feature-expert-handoff-contract
    return handoff_check.problems(text, _resolver(slug))
