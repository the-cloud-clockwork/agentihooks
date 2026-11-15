from typing import Any


def claim_without_admitted_execution(agent: Any, doc: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    from scripts.swarm import profile_choice

    # ruleid: admission-identity-skips-admitted-execution
    identity = {
        "execution_id": agent.execution_id,
        "execution_generation": agent.generation,
        profile_choice.LEDGER_INTENT: profile_choice.carried(doc, task),
    }
    return identity


def claim_with_admitted_execution(agent: Any, doc: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    from scripts.swarm import profile_choice

    # ok: admission-identity-skips-admitted-execution
    identity = {
        "execution_id": agent.execution_id,
        "admitted_execution_id": agent.execution_id,
        "execution_generation": agent.generation,
        profile_choice.LEDGER_INTENT: profile_choice.carried(doc, task),
    }
    return identity
