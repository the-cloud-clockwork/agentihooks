from dataclasses import dataclass, field, replace

from scripts.ci_lab import scheduler
from scripts.ci_lab.intake import GradeRequest


@dataclass
class Expander:
    plans: dict[str, scheduler.Plan]
    seen: list[GradeRequest] = field(default_factory=list[GradeRequest])

    def expand(self, request: GradeRequest) -> scheduler.Plan:
        self.seen.append(request)
        plan = self.plans[request.head]
        scope = plan.outputs.get("select", {}).get("scope", "none")
        return replace(
            plan, required=scheduler._frame_required(int(request.tier), scope), graded_commit=request.graded_commit
        )

    def merge_base(self, repo: str, head: str) -> str:
        return self.plans[head].grader

    def reachable(self, repo: str, head: str) -> bool:
        return True

    def groups(self, repo: str) -> frozenset[str]:
        return frozenset(self.plans)
