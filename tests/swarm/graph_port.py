"""A source control port double whose graph tool hands the gh argv to a run fake, and the pull request reads over it."""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from scripts.swarm import github_reads
from scripts.swarm.ledger_events import view as read_view
from scripts.swarm.ledger_events import views as read_views
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import SourceControl


@dataclass(frozen=True)
class RunPort:
    """Answer graph through a run fake the way the GitHub adapter answers it through the charged gh reader."""

    run: Callable[..., Any]

    def graph(self, document: str, variables: dict[str, str], *, deadline: Deadline) -> Any:
        """Run the document through the fake and refuse a failed answer as the adapter does."""
        flags = [word for name, value in variables.items() for word in ("-f", f"{name}={value}")]
        args = ["api", "graphql", "-f", f"query={document}", *flags]
        caller = github_reads.CALLER.get("tick.source_control")
        options = {"capture_output": True, "text": True, "timeout": round(deadline.remaining)}
        done = github_reads.rest(caller, args, run=self.run, **options)
        if done.returncode:
            raise subprocess.CalledProcessError(done.returncode, args, done.stdout, getattr(done, "stderr", None))
        return json.loads(done.stdout)


def view(url: str, run: Callable[..., Any], required_checks: dict[str, int] | None = None) -> Any:
    """Read one pull request through the port double over ``run``."""
    return read_view(url, cast(SourceControl, RunPort(run)), required_checks)


def views(urls: list[str], run: Callable[..., Any], cache: object | None = None) -> Any:
    """Read pull requests in batches through the port double over ``run``."""
    return read_views(urls, cast(SourceControl, RunPort(run)), cache)
