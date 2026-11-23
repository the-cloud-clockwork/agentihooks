"""The lab remote's post receive hook: each pushed `refs/grade/*` head becomes a lab grade and the pusher reads its url.

git runs the hook after a push lands, with one `<old> <new> <ref>` line per updated ref on stdin and the push options
in GIT_PUSH_OPTION_COUNT and GIT_PUSH_OPTION_<n>. Each pushed grade ref is published on the bus as a
`ci.lab.head.notice` envelope, which the scheduler's inbox grades, carrying the ref and the `branch=<name>` push option
for the relay. Its base is the `base=<sha>` push option, else the head's first parent. The hook prints the grade url
`<CI_LAB_URL>/grades/<sha>`, which git shows the pusher as a `remote:` line. A deleted grade ref, a grade head with no
base and any other ref are not graded, each with a line.
"""

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, replace
from typing import TextIO

from scripts.ci_lab.contract import LAB_URL
from scripts.ci_lab.webhooks import NOTICE, PUSHED, HeadEvent, Publisher, envelope
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus

REPOSITORY = "CI_LAB_REPOSITORY"
ACTOR = "CI_LAB_REMOTE_ACTOR"
OPTION_COUNT = "GIT_PUSH_OPTION_COUNT"
OPTION = "GIT_PUSH_OPTION_"
GRADE_REFS = "refs/grade/"
BRANCH = "branch"
BASE = "base"


def push_options(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the push's options as name to value, each given as `name=value`; a bare name maps to empty text."""
    count = int(environ.get(OPTION_COUNT) or 0)
    pairs = (environ[f"{OPTION}{index}"].partition("=") for index in range(count))
    return {name: value for name, _, value in pairs}


def first_parent(sha: str) -> str:
    """Return the first parent of a commit in the repository git runs the hook in, or empty text for a root commit."""
    return subprocess.run(["git", "rev-parse", "--verify", f"{sha}^"], capture_output=True, text=True).stdout.strip()


def grade(event: HeadEvent, ref: str, branch: str) -> Envelope:
    """Return the envelope of a pushed grade head carrying its ref and branch; its id covers both, so the same head
    pushed to another grade ref or with another branch is a new head event, not a resend the bus drops."""
    data = {**asdict(event), "ref": ref, BRANCH: branch}
    return replace(envelope(event), id=hashlib.sha256(json.dumps(data).encode()).hexdigest(), data=data)


def run(
    lines: Iterable[str],
    environ: Mapping[str, str],
    events: Publisher,
    parent: Callable[[str], str],
    out: TextIO,
) -> int:
    """Publish a lab head for each pushed grade ref and print its grade url; every other ref update is not graded.

    git has written the refs before the hook runs, so the hook cannot turn a push away; the lab remote's pre receive
    hook refuses refs outside refs/grade/*. A grade head with no base, a root commit pushed without the base option,
    is not graded either.
    """
    options = push_options(environ)
    lab = environ[LAB_URL].rstrip("/")
    for line in lines:
        if not line.strip():
            continue
        _, new, ref = line.split()
        graded = ref.startswith(GRADE_REFS) and new != "0" * len(new)
        base = (options.get(BASE) or parent(new)) if graded else ""
        if not base:
            out.write(f"lab remote: {ref} is not graded, only a pushed refs/grade/* head with a base is\n")
            continue
        branch = options.get(BRANCH, "")
        event = HeadEvent(NOTICE, environ[REPOSITORY], new, base, environ[ACTOR], PUSHED, branch)
        head = grade(event, ref, branch)
        events.publish(head.type, head)
        out.write(f"lab grade: {lab}/grades/{new}\n")
    return 0


def main(environ: Mapping[str, str] = os.environ) -> int:
    """Run the hook over git's stdin, publishing on the bus the AGENTIHOOKS_BUS settings name."""
    bus = open_bus(replace(BusConfig.from_environ(environ), provision=False))
    try:
        return run(sys.stdin, environ, bus.lab_heads, first_parent, sys.stdout)
    finally:
        bus.close()


if __name__ == "__main__":
    sys.exit(main())
