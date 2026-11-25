def refused_while_starting(agent: object, name: str) -> object:
    if agent.execution_id == "":
        return None
    # ruleid: codex-attach-before-classification
    if not capabilities.of_agent(agent).native_terminal_attach:
        raise AttachError(NO_ATTACH.format(name))
    return live_binding.transport(store, agent)


def pending_while_starting(agent: object, name: str) -> object:
    if agent.execution_id == "":
        return None
    if agent.state == ADMITTED and not agent.harness:
        return None
    # ok: codex-attach-before-classification
    if not capabilities.of_agent(agent).native_terminal_attach:
        raise AttachError(NO_ATTACH.format(name))
    return live_binding.transport(store, agent)
