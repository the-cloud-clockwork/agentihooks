from typing import Any

from scripts.codex_session.attach import LAUNCHING_HARNESSES, NO_ATTACH, AttachError
from scripts.swarm import capabilities


def premature_rejection(agent: Any) -> None:
    # ruleid: codex-attach-before-classification
    if not capabilities.of_agent(agent).native_terminal_attach:
        raise AttachError(NO_ATTACH.format(agent.name))


def wait_before_rejection(agent: Any) -> None:
    # ok: codex-attach-before-classification
    if not capabilities.of_agent(agent).native_terminal_attach:
        if agent.state == "starting" and agent.harness in LAUNCHING_HARNESSES:
            return None
        raise AttachError(NO_ATTACH.format(agent.name))
