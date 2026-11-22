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
    found = handoff_contracts.problems(text, Resolver(args.slug, store.redis, ledger.item), store, args.slug, agent)
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
    if not _expert(environ):
        # ok: feature-expert-handoff-contract
        return handoff_check.problems(text, _resolver(slug))
    store = connect()
    return contracts.problems(text, Resolver(slug, store.redis), store, slug, agent)


def _problems(text, slug, environ):
    resolves = _resolver(slug)
    if _expert(environ):
        # ruleid: feature-expert-handoff-contract
        return handoff_check.problems(text, resolves)
    return contracts.problems(text, resolves, connect(), slug, agent)


def _problems(text, slug, environ):
    # ruleid: feature-expert-handoff-contract
    return handoff_check.problems(text, _resolver(slug))


def _problems(text, slug, environ):
    if not _expert(environ):
        return contracts.problems(text, _resolver(slug), connect(), slug, agent)
    else:
        # ruleid: feature-expert-handoff-contract
        return handoff_check.problems(text, _resolver(slug))


def gate(tool_name, tool_input, session_id, environ=None):
    text = Path(tool_input["file_path"]).read_text(encoding="utf-8")
    # ruleid: feature-expert-handoff-contract
    found = handoff_check.problems(text, _resolver(slug))
    return "BLOCKED: " + handoff_check.refusal(found) if found else None


def _checked(text, slug):
    # ruleid: feature-expert-handoff-contract
    return handoff_check.problems(text, Resolver(slug, None))


def _quick_refusal(rest, found):
    text = document.read_text(encoding="utf-8")
    # ok: feature-expert-handoff-contract
    problems = handoff_check.problems(text, quick_handoff.resolver(found.folder))
    return "BLOCKED: " + handoff_check.refusal(problems) if problems else None
