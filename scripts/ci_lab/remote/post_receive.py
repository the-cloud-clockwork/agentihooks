"""The lab remote's post receive hook: each pushed `refs/grade/*` head becomes a lab grade and the pusher reads its url.

git runs the hook after a push lands, with one `<old> <new> <ref>` line per updated ref on stdin and the push options
in GIT_PUSH_OPTION_COUNT and GIT_PUSH_OPTION_<n>. Each pushed grade ref is published on the bus as a
`ci.lab.head.notice` envelope, which the scheduler's inbox grades, carrying the ref and the `branch=<name>` push option
for the relay. Its base is the `base=<sha>` push option, else the head's first parent. The hook prints the grade url
`<CI_LAB_URL>/grades/<sha>`, which git shows the pusher as a `remote:` line. A deleted grade ref and any other ref
are refused with a line and grade nothing.
"""

import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from typing import TextIO

from scripts.ci_lab.webhooks import NOTICE, PUSHED, HeadEvent, Publisher, envelope

REPOSITORY = "CI_LAB_REPOSITORY"
ACTOR = "CI_LAB_REMOTE_ACTOR"
LAB_URL = "CI_LAB_URL"
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
    """Return the first parent of a commit in the repository git runs the hook in."""
    done = subprocess.run(["git", "rev-parse", "--verify", f"{sha}^"], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def run(
    lines: Iterable[str],
    environ: Mapping[str, str],
    events: Publisher,
    parent: Callable[[str], str],
    out: TextIO,
) -> int:
    """Publish a lab head for each pushed grade ref and print its grade url; refuse every other ref update."""
    options = push_options(environ)
    lab = environ[LAB_URL].rstrip("/")
    for line in lines:
        if not line.strip():
            continue
        _, new, ref = line.split()
        if not ref.startswith(GRADE_REFS) or not new.strip("0"):
            print(f"lab remote: refused {ref}, only a pushed refs/grade/* head is graded", file=out)
            continue
        base = options.get(BASE) or parent(new)
        head = envelope(HeadEvent(NOTICE, environ[REPOSITORY], new, base, environ[ACTOR], PUSHED))
        head = replace(head, data={**head.data, "ref": ref, BRANCH: options.get(BRANCH, "")})
        events.publish(head.type, head)
        print(f"lab grade: {lab}/grades/{new}", file=out)
    return 0


def main(environ: Mapping[str, str] = os.environ) -> int:
    """Run the hook over git's stdin, publishing on the bus the AGENTIHOOKS_BUS settings name."""
    from scripts.events.config import BusConfig
    from scripts.events.factory import open_bus

    bus = open_bus(replace(BusConfig.from_environ(environ), provision=False))
    try:
        return run(sys.stdin, environ, bus.lab_heads, first_parent, sys.stdout)
    finally:
        bus.close()


if __name__ == "__main__":
    sys.exit(main())
