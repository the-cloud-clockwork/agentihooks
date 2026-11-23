from argparse import Namespace

from scripts import review
from scripts.swarm.store import RedisStore


class MissingFindingsCheck:
    # ruleid: feature-review-findings-queue-gate
    def cmd_done(self, store: RedisStore, args: Namespace) -> None:
        return None


class CheckedFindings:
    # ok: feature-review-findings-queue-gate
    def cmd_done(self, store: RedisStore, args: Namespace) -> None:
        review.require_done(store, args.slug, "review", args.task)
