import json
import sys
import time

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.swarm_ledger import ledger_duplicates

ERRORS = {"unavailable": lambda: ClassifierUnavailable("down"), "KeyError": KeyError, "TimeoutError": TimeoutError}


def judge(yes, error, hold):
    def decide(state, questions, *, purpose):
        time.sleep(hold)
        if error:
            raise ERRORS[error]()
        return DecisionResult(
            {
                name: Answer("noul", noul=0.9 if any(f" {i} " in q.instructions for i in yes) else 0.05)
                for name, q in questions.items()
            },
            "unit-test",
        )

    return decide


if __name__ == "__main__":
    yes, error, hold = json.loads(sys.argv[1])
    ledger_duplicates.decide = judge(yes, error, hold)
    ledger_duplicates.main()
