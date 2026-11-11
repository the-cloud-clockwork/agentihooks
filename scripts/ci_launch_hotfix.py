"""Grade a dev push for the launch hotfix route: when the pull request merged as this commit carries the launch hotfix
label since it last entered the merge queue, its own required grade passed on its head and this run's unit suite
passed, promotion proceeds past unrelated red promotion checks. Those checks keep their red result, so the promotion
watch still files their fixer tasks.

Prints ``promote=true`` or ``promote=false`` for the job output; reads the run's job results from ``NEEDS``.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

LABEL = "launch hotfix"
SUITE = ("select", "split", "shard-check", "test-count")
UNIT = ("unit", "unit-lost", "unit-rerun")
UNIT_PASS = ("success", "skipped", "skipped")
GRADE = "Gate — Required"


def suite_passed(needs: Mapping[str, Any]) -> bool:
    """Whether the unit suite and its selection, split, shard and count checks passed on this run."""
    results = {name: (needs.get(name) or {}).get("result") for name in (*SUITE, *UNIT)}
    return all(results[name] == "success" for name in SUITE) and tuple(results[name] for name in UNIT) == UNIT_PASS


def hotfix_pulls(pulls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The commit's merged pull requests that carry the launch hotfix label."""
    return [
        pull
        for pull in pulls
        if pull.get("merged_at") and any(label.get("name") == LABEL for label in pull.get("labels") or [])
    ]


def labelled_at_queue(events: list[dict[str, Any]]) -> bool:
    """Whether the launch hotfix label was on the pull request when it last entered the merge queue."""
    labelled = queued = False
    for event in events:
        kind, name = event.get("event"), (event.get("label") or {}).get("name")
        if kind in ("labeled", "unlabeled") and name == LABEL:
            labelled = kind == "labeled"
        elif kind == "added_to_merge_queue":
            queued = labelled
    return queued


def timeline_of(repo: str, number: int, run: Callable[..., Any] = subprocess.run) -> list[dict[str, Any]]:
    """The pull request's timeline events, oldest first."""
    argv = ["gh", "api", f"repos/{repo}/issues/{number}/timeline?per_page=100", "--paginate", "--jq", ".[] | @json"]
    done = run(argv, capture_output=True, text=True, check=True)
    return [json.loads(line) for line in done.stdout.splitlines() if line]


def head_green(repo: str, head: str, run: Callable[..., Any] = subprocess.run) -> bool:
    """Whether the pull request's own required grade passed on its head commit."""
    path = f"repos/{repo}/commits/{head}/check-runs?check_name={quote(GRADE)}"
    done = run(["gh", "api", path, "--jq", ".check_runs[].conclusion"], capture_output=True, text=True, check=True)
    return "success" in done.stdout.split()


def pulls_of(repo: str, sha: str, run: Callable[..., Any] = subprocess.run) -> list[dict[str, Any]]:
    """The pull requests GitHub associates with the commit."""
    done = run(["gh", "api", f"repos/{repo}/commits/{sha}/pulls"], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def main(
    argv: list[str] | None = None, run: Callable[..., Any] = subprocess.run, environ: Mapping[str, str] = os.environ
) -> int:
    """Print the promote output for the dev push commit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args(argv)
    needs = json.loads(environ.get("NEEDS") or "{}")
    try:
        pulls = hotfix_pulls(pulls_of(args.repo, args.sha, run)) if suite_passed(needs) else []
        promote = any(
            labelled_at_queue(timeline_of(args.repo, pull["number"], run))
            and head_green(args.repo, pull["head"]["sha"], run)
            for pull in pulls
        )
    except subprocess.CalledProcessError as exc:
        print(f"launch hotfix label unreadable, promoting on the full grade only: {exc.stderr}", file=sys.stderr)
        promote = False
    print(f"promote={'true' if promote else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
