from pathlib import Path


def missing_export(ledger: Path, home: Path) -> tuple[Path, ...]:
    # ruleid: codex-planner-export-grant-missing
    return (ledger, home / ".agentihooks" / "swarm", home / "scratchpad")


def with_export(ledger: Path, home: Path, exports: Path) -> tuple[Path, ...]:
    # ok: codex-planner-export-grant-missing
    return (ledger, home / ".agentihooks" / "swarm", home / "scratchpad", exports)
