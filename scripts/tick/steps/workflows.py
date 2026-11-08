"""The workflows steps of the tick."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.swarm import workflow_runner
from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep, pass_domains


def _workflow_runner(tick: Any, get: Callable[[str], Any]) -> list[str]:
    return workflow_runner.advance_tasks(
        get("inbox"), get("slug"), get("doc"), Path(get("store").config(get("slug")).repo)
    )


registry.register(
    CallStep(
        "workflow runner",
        "wrapper phases",
        _workflow_runner,
        needs=frozenset({"planning and phase pass", "ledger state"}),
        reads=pass_domains("inbox", "slug", "doc", "store"),
        writes=frozenset(),
        lane=Lane("slow"),
        effects=frozenset({"host", "inbox", "llm"}),
        feature="swarm-workflow-runner",
    )
)
