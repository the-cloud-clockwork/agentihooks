from hooks.context import scheduled_prompts


def is_operator_prompt(prompt: str, slug: str) -> bool:
    return bool(prompt)


def heard(payload: dict) -> bool:
    return True


def typed_unchecked(payload: dict, env: dict) -> bool:
    if env.get("AGENTIHOOKS_AGENT_NAME"):
        # ruleid: presence-counts-scheduled-prompts
        return heard(payload)
    # ruleid: presence-counts-scheduled-prompts
    return is_operator_prompt(payload.get("prompt", ""), env.get("AGENTIHOOKS_SWARM"))


def typed_checked(payload: dict, env: dict) -> bool:
    if scheduled_prompts.fired(payload.get("session_id") or "", payload.get("prompt")):
        return False
    if env.get("AGENTIHOOKS_AGENT_NAME"):
        # ok: presence-counts-scheduled-prompts
        return heard(payload)
    # ok: presence-counts-scheduled-prompts
    return is_operator_prompt(payload.get("prompt", ""), env.get("AGENTIHOOKS_SWARM"))
