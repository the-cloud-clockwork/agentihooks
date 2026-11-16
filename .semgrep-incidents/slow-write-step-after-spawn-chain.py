from scripts.tick.engine import SLOW
from scripts.tick.steps import budgets
from scripts.tick.steps.budgets import SPAWN_NEEDS
from scripts.tick.steps.tasks import REOPENS, RESUMED, WriteStep, held, pass_domains

# ruleid: slow-write-step-after-spawn-chain
LATE = WriteStep(
    "late notice",
    REOPENS,
    held(lambda tick, get: []),
    writes=frozenset(),
    needs=budgets.SPAWN_NEEDS,
    reads=pass_domains("rows"),
    lane=SLOW,
)

# ruleid: slow-write-step-after-spawn-chain
IMPORTED = WriteStep("imported notice", REOPENS, held(lambda tick, get: []), needs=SPAWN_NEEDS, lane=SLOW)

# ok: slow-write-step-after-spawn-chain
EARLY = WriteStep(
    "early notice",
    REOPENS,
    held(lambda tick, get: []),
    writes=frozenset(),
    needs=RESUMED,
    reads=pass_domains("rows"),
    lane=SLOW,
)

# ok: slow-write-step-after-spawn-chain
CRITICAL = WriteStep("critical notice", REOPENS, held(lambda tick, get: []), needs=budgets.SPAWN_NEEDS)
