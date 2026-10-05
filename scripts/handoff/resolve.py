"""Whether a Read first address names something that exists: a GitHub issue or pull request, a ledger item, a task
work folder note, a seat recap or an inbox item."""

import re
import subprocess

from scripts.inbox.seats import SeatMemory
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_workspace

GITHUB = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)")
NOTES = ("steering", "progress", "proof")


def _ledger_state(slug):
    from scripts.swarm.ledger_client import LedgerClient

    return LedgerClient().state(slug)


class Resolver:
    def __init__(self, slug, redis, ledger_state=_ledger_state, run=subprocess.run):
        self.slug, self.redis, self.ledger_state, self.run = slug, redis, ledger_state, run

    def __call__(self, address):
        scheme, _, rest = address.partition(":")
        check = {
            "https": lambda: self._github(address),
            "ledger": lambda: self._ledger(rest),
            "workspace": lambda: self._workspace(rest),
            "recap": lambda: self.redis is not None and bool(SeatMemory(self.redis).recaps(rest)),
            "inbox": lambda: self.redis is not None and self._inbox(rest),
        }.get(scheme)
        return bool(check and check())

    def _github(self, address):
        match = GITHUB.fullmatch(address)
        if not match:
            return False
        owner, repo, number = match.groups()
        try:
            done = self.run(
                ["gh", "api", f"repos/{owner}/{repo}/issues/{number}", "--silent"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0

    def _ledger(self, rest):
        slug, _, rest = rest.partition("/")
        kind, _, item = rest.partition("/")
        try:
            state = self.ledger_state(slug)
        except (SwarmError, OSError):
            return False
        return any(isinstance(entry, dict) and entry.get("id") == item for entry in state.get(kind) or [])

    def _workspace(self, rest):
        task, _, note = rest.partition("/")
        try:
            folder = ledger_workspace.folder(self.slug, task)
        except ValueError:
            return False
        return note in NOTES and (folder / f"{note}.md").is_file()

    def _inbox(self, item_id):
        try:
            InboxStore(self.redis).get(item_id)
        except InboxError:
            return False
        return True
