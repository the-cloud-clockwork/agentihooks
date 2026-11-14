import json
import sys
from typing import Any


class BareReport:
    def spawn(self, claims: Any, key: str, started: Any, placed: Any, revision: int) -> Any:
        claims.update(key, json.dumps({"state": "started"}).encode(), revision)
        # ruleid: hive-launch-report-unguarded
        self.tasks.launched(started, placed)
        return started


class GuardedReport:
    def report(self, event: Any, launch: Any) -> None:
        try:
            # ok: hive-launch-report-unguarded
            self.tasks.launched(event, launch)
        except Exception as exc:
            print(f"launch report failed ({exc})", file=sys.stderr)
