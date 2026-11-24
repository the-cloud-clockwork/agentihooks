from argparse import Namespace

from scripts import review
from scripts.swarm.store import RedisStore
from scripts.swarm_ledger import ledger_kinds


class MissingFindingsCheck:
    # ruleid: feature-review-done-findings
    def cmd_done(self, store: RedisStore, args: Namespace) -> None:
        missing = ledger_kinds.unmet({})
        print(missing)


class FindingsCheckedAfterProof:
    # ruleid: feature-review-done-findings
    def cmd_done(self, store: RedisStore, args: Namespace) -> None:
        missing = ledger_kinds.unmet({})
        review.require_done(store, args.slug, "review", "review", args.task)
        print(missing)


class FindingsCheckedBeforeProof:
    # ok: feature-review-done-findings
    def cmd_done(self, store: RedisStore, args: Namespace) -> None:
        review.require_done(store, args.slug, "review", "review", args.task)
        missing = ledger_kinds.unmet({})
        print(missing)
