from typing import Any

from scripts import ci_budget

# ruleid: ci-stage-budget-direct-table-read
from scripts.ci_budget import BUDGETS as BUDGETS


def stored_metric(stage: str) -> int:
    # ruleid: ci-stage-budget-direct-table-read
    return ci_budget.BUDGETS.get(stage, 0)


def stored_evidence(stage: str) -> int:
    # ruleid: ci-stage-budget-direct-table-read
    return ci_budget.BUDGETS[stage]


def stored_membership(stage: str) -> bool:
    # ruleid: ci-stage-budget-direct-table-read
    return stage in ci_budget.BUDGETS


def collected(stage: str, jobs: list[dict[str, Any]]) -> int | None:
    # ok: ci-stage-budget-direct-table-read
    return ci_budget.budget(stage, jobs)
