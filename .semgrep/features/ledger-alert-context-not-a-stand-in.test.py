from types import SimpleNamespace

from scripts.swarm_ledger import ledger_alerts, ledger_core


def admit_with_a_stand_in(state: dict, now_ms: int, before: list, text: str) -> None:
    # ruleid: feature-ledger-alert-context-not-a-stand-in
    raised = SimpleNamespace(rev=state["_meta"]["rev"], at=now_ms)
    ledger_alerts.raise_warning(state, raised, (ledger_alerts.WRITER, text), before)


def new_alert_with_a_stand_in(state: dict, now_ms: int, text: str) -> None:
    # ruleid: feature-ledger-alert-context-not-a-stand-in
    raised = SimpleNamespace(rev=state["_meta"]["rev"], at=now_ms)
    ledger_alerts.new_alert(state, raised, "writer", text, "master")


def admit_with_an_inline_stand_in(state: dict, now_ms: int, before: list, text: str) -> None:
    # ruleid: feature-ledger-alert-context-not-a-stand-in
    ledger_alerts.raise_warning(
        state, SimpleNamespace(rev=state["_meta"]["rev"], at=now_ms), (ledger_alerts.WRITER, text), before
    )


def admit_through_warning_raised(state: dict, rev: int, at: int, before: list, text: str) -> None:
    # ok: feature-ledger-alert-context-not-a-stand-in
    ledger_alerts.warning_raised(state, rev, at, (ledger_alerts.WRITER, text), before)


def admit_with_the_real_context(state: dict, rev: int, at: int, before: list, text: str) -> None:
    # ok: feature-ledger-alert-context-not-a-stand-in
    context = ledger_core.Context({"rev": rev - 1, "stamps": {}}, at)
    ledger_alerts.raise_warning(state, context, (ledger_alerts.WRITER, text), before)
