"""The CI rerun budget: at most two reruns per pull request head; a rerun of jobs cancelled before a runner took them is free."""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath

from scripts.gates import log
from scripts.gates.base import Decision
from scripts.gates.budget import Budget
from scripts.gates.identity import program_index, simple_commands

PASSED = frozenset({"success", "skipped", "neutral"})
VALUE_FLAGS = {"-j": "job", "--job": "job", "-R": "repo", "--repo": "repo"}
GH_TIMEOUT_SEC = 8
RERUN_CAP = 2
REST_RERUN = re.compile(
    r"(?:^|/)repos/(?P<repo>[^/]+/[^/]+)/actions/(?P<kind>runs|jobs)/(?P<id>\d+)/rerun(?:-failed-jobs)?$"
)


@dataclass(frozen=True)
class Target:
    run: str = ""
    job: str = ""
    repo: str = ""


def _target(args):
    found, words = {"run": "", "job": "", "repo": ""}, iter(args)
    for word in words:
        flag, eq, value = word.partition("=")
        if flag in VALUE_FLAGS:
            found[VALUE_FLAGS[flag]] = value if eq else next(words, "")
        elif not (found["run"] or flag.startswith("-")):
            found["run"] = flag
    return Target(**found)


def _rest_target(args):
    for word in args:
        found = REST_RERUN.search(word)
        if found:
            return Target(**{found["kind"][:-1]: found["id"], "repo": found["repo"]})
    return None


def targets(command):
    for words in simple_commands(command):
        index = program_index(words)
        if index is None or PurePosixPath(words[index]).name != "gh":
            continue
        if words[index + 1 : index + 3] == ["run", "rerun"]:
            yield _target(words[index + 3 :])
        elif words[index + 1 : index + 2] == ["api"]:
            found = _rest_target(words[index + 2 :])
            if found:
                yield found


def _api(path, run):
    out = run(["gh", "api", path], capture_output=True, text=True, check=True, timeout=GH_TIMEOUT_SEC)
    return json.loads(out.stdout)


def gh_jobs(target, run=subprocess.run):
    repo = target.repo or "{owner}/{repo}"
    if target.job:
        return [_api(f"repos/{repo}/actions/jobs/{target.job}", run)]
    if target.run:
        return _api(f"repos/{repo}/actions/runs/{target.run}/jobs?per_page=100", run)["jobs"]
    return []


def exempt(jobs):
    unsuccessful = [job for job in jobs if job.get("conclusion") not in PASSED]
    return bool(unsuccessful) and all(
        job.get("conclusion") == "cancelled" and not job.get("runner_name") for job in unsuccessful
    )


def refusal(slug, head, cap):
    return (
        f"CI reruns on pull request head {head[:12]} are spent: {cap} of {cap}. Read the failed job's log and push "
        f'a fix, or block with agentihooks swarm {slug} block "<why>"'
    )


class RerunBudget:
    name = "reruns"
    default_mode = "enforce"

    def __init__(self, cap=RERUN_CAP, jobs=gh_jobs):
        self.cap, self.jobs = cap, jobs

    def matches(self, call):
        return call.tool == "Bash" and "gh" in call.command and "rerun" in call.command

    def decide(self, call, who, state):
        if not who.pinned:
            return Decision()
        for target in targets(call.command):
            found = self.jobs(target) if target.run or target.job else []
            if not found or exempt(found):
                continue
            head = found[0]["head_sha"]
            allowed, spent = Budget(state.slug, self.name, state.home).spend(head, "reruns", self.cap)
            if not allowed:
                return Decision.deny(refusal(who.swarm, head, self.cap))
            reason = f"CI reruns {spent} of {self.cap} on head {head[:12]}"
            log.append(state.slug, log.Row.of(self.name, "count", who, call.tool, reason), state.home)
        return Decision()
