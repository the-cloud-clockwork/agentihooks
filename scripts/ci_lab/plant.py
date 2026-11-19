"""Both ways plant proofs on the deployed lab: python -m scripts.ci_lab grade --plant NAME.

A plant is tests/ci_lab/plants/NAME.json: the static gate job it turns red, the reason, and edits that each replace one
exact text once. The command cuts a twin commit carrying the base tree and the plant commit carrying the edits on top,
both with a fixed identity and the base's commit time so a rerun on the same base cuts the same commits, and points
diffcheck/ci-lab-NAME at the plant commit. It sends the lab a signed head notice for each against the base, so both
grade in the pull request class with the base as the grader, and reads the lab check runs of both grades from GitHub
by their grade run. The proof holds when the twin's required check succeeds and the plant's fails with the plant's
gate its only failing gate: it prints the proof table row with the check run links and exits 0, else 1, and 2 when
the proof cannot run.
"""

import argparse
import hashlib
import hmac
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from itertools import count
from pathlib import Path
from typing import Any

import requests

from scripts.ci_lab.checks import API, AUTHORITATIVE, HEADERS, SHADOW, TIMEOUT
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.digests import GradeInputs, grade_key
from scripts.ci_lab.scheduler import REQUIRED
from scripts.ci_lab.units import Priority
from scripts.ci_lab.webhooks import NOTICE_FIELDS, NOTICE_SECRET_ENV
from scripts.hub.github_webhooks import NOTICE_PATH, PORT

PLANTS = Path("tests/ci_lab/plants")
BRANCH = "diffcheck/ci-lab-"
NOTICE_URL = f"http://agentihooks-swarm-github-webhook.agentihooks-swarm.svc:{PORT}{NOTICE_PATH}"
IDENTITY = ("agentihooks lab plant", "lab-plant@agentihooks.invalid")
PREFIXES = (AUTHORITATIVE, SHADOW)
SUCCESS = "success"
FAILURE = "failure"
COMPLETED = "completed"
PAGE = 100
WAIT_S = 1800.0
POLL_S = 20.0
HEADER = "| Plant | Gate | Reason | Without the plant | With the plant |\n|---|---|---|---|---|"


class ProofError(Exception):
    """A step of a proof the lab or GitHub refused."""


@dataclass(frozen=True)
class Edit:
    """One exact text of a file a plant replaces once."""

    path: str
    before: str
    after: str


@dataclass(frozen=True)
class Plant:
    """A planted fault: its name, the static gate job it turns red, why, and its edits."""

    name: str
    gate: str
    reason: str
    edits: tuple[Edit, ...]


@dataclass(frozen=True)
class Heads:
    """The base a plant grades against, the twin carrying the base tree and the plant commit on top of the twin."""

    base: str
    twin: str
    plant: str


@dataclass(frozen=True)
class Check:
    """One lab check run of a grade: its status, conclusion and page."""

    status: str
    conclusion: str | None
    url: str


@dataclass(frozen=True)
class Ports:
    """The calls a proof makes outside git: post a notice for its status code, read a GitHub API path, and sleep."""

    post: Callable[[str, bytes, Mapping[str, str]], int]
    get: Callable[[str], Any]
    sleep: Callable[[float], None]


@dataclass(frozen=True)
class Settings:
    """The repository and actor a proof notices for, the notice secret, the base and remote, and its lab waits."""

    repository: str
    actor: str
    secret: bytes
    base: str
    remote: str
    notice_url: str
    wait_s: float
    poll_s: float


def load(root: Path, name: str) -> Plant:
    """Return the plant of that name under the root."""
    data = json.loads((root / PLANTS / f"{name}.json").read_text())
    edits = tuple(Edit(str(edit["path"]), str(edit["before"]), str(edit["after"])) for edit in data["edits"])
    return Plant(name, str(data["gate"]), str(data["reason"]), edits)


def planted(text: str, edit: Edit) -> str:
    """Return the text with the edit's exact text replaced; refuse a text that holds it other than once."""
    found = text.count(edit.before)
    if found != 1:
        raise ProofError(f"{edit.path} holds the plant's text {found} times, not once")
    return text.replace(edit.before, edit.after)


def _git(root: Path, *argv: str, env: Mapping[str, str] | None = None, stdin: bytes | None = None) -> str:
    """Return the stripped output of a git command in the root, with extra environment and standard input."""
    extra: dict[str, str] = dict(env or {})
    done = subprocess.run(
        ["git", "-C", str(root), *argv], input=stdin, capture_output=True, check=True, env={**os.environ, **extra}
    )
    return done.stdout.decode().strip()


def _show(root: Path, revision: str, path: str) -> str:
    """Return one file of a revision, unstripped."""
    done = subprocess.run(["git", "-C", str(root), "show", f"{revision}:{path}"], capture_output=True, check=True)
    return done.stdout.decode()


def _identity(root: Path, base: str) -> dict[str, str]:
    """Return the plant author and committer, dated at the base's commit time."""
    when = f"@{_git(root, 'log', '-1', '--format=%ct', base)} +0000"
    name, email = IDENTITY
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_COMMITTER_DATE": when,
    }


def commits(root: Path, plant: Plant, base: str) -> Heads:
    """Return the twin commit carrying the base tree and the plant commit carrying the plant's edits on the twin."""
    base = _git(root, "rev-parse", "--verify", f"{base}^{{commit}}")
    who = _identity(root, base)
    tree = _git(root, "rev-parse", f"{base}^{{tree}}")
    twin = _git(root, "commit-tree", tree, "-p", base, "-m", f"Lab plant {plant.name}: the base tree", env=who)
    texts: dict[str, str] = {}
    for edit in plant.edits:
        texts[edit.path] = planted(texts.get(edit.path) or _show(root, base, edit.path), edit)
    with tempfile.TemporaryDirectory() as folder:
        index = {"GIT_INDEX_FILE": str(Path(folder) / "index")}
        _git(root, "read-tree", tree, env=index)
        for path, text in texts.items():
            mode = _git(root, "ls-tree", base, "--", path).split()[0]
            blob = _git(root, "hash-object", "-w", "--stdin", stdin=text.encode())
            _git(root, "update-index", "--cacheinfo", f"{mode},{blob},{path}", env=index)
        edited = _git(root, "write-tree", env=index)
    head = _git(root, "commit-tree", edited, "-p", twin, "-m", f"Lab plant {plant.name}: {plant.reason}", env=who)
    return Heads(base, twin, head)


def push(root: Path, remote: str, heads: Heads, name: str) -> str:
    """Point the plant's branch on the remote at the plant commit and return the branch; a branch already there is
    left alone, and one at another commit is deleted first so the push names no earlier plant as its base."""
    branch = f"{BRANCH}{name}"
    listed = _git(root, "ls-remote", remote, f"refs/heads/{branch}").split()
    if listed[:1] == [heads.plant]:
        return branch
    if listed:
        _git(root, "push", remote, "--delete", branch)
    _git(root, "push", remote, f"{heads.plant}:refs/heads/{branch}")
    return branch


def notice(repository: str, head: str, base: str, actor: str) -> bytes:
    """Return the head notice naming a head of the repository against its base for the actor."""
    return json.dumps(dict(zip(NOTICE_FIELDS, (repository, head, base, actor)))).encode()


def signature(secret: bytes, body: bytes) -> str:
    """Return the X-Hub-Signature-256 value the receiver checks a notice with."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def run_key(repository: str, head: str, base: str) -> str:
    """Return the grade run a notice opens: a pull request class grade of the head graded by the base."""
    return grade_key(GradeInputs(repository, head, base, CLASSES[Priority.PULL_REQUEST], base))


def checks(ports: Ports, slug: str, head: str, run: str) -> dict[str, Check]:
    """Return the lab check runs of one grade run on a head by gate, from every page GitHub lists."""
    found: dict[str, Check] = {}
    for page in count(1):
        listed = ports.get(f"repos/{slug}/commits/{head}/check-runs?filter=all&per_page={PAGE}&page={page}")
        runs = listed["check_runs"]
        for item in runs:
            prefix, _, gate = str(item["name"]).partition("/")
            if prefix in PREFIXES and item["external_id"] == run:
                found[gate] = Check(str(item["status"]), item["conclusion"], str(item["html_url"]))
        if len(runs) < PAGE:
            break
    return found


def settled(ports: Ports, slug: str, head: str, run: str, wait_s: float, poll_s: float) -> dict[str, Check]:
    """Return a grade's check runs once its required check completed, reading every poll until the wait ends."""
    polls = math.ceil(wait_s / poll_s)
    for read in range(polls + 1):
        found = checks(ports, slug, head, run)
        if REQUIRED in found and found[REQUIRED].status == COMPLETED:
            return found
        if read < polls:
            ports.sleep(poll_s)
    raise ProofError(f"the lab completed no {REQUIRED} check of grade {run} on {head} within {wait_s:g} seconds")


def problems(plant: Plant, twin: Mapping[str, Check], head: Mapping[str, Check]) -> list[str]:
    """Return why a proof fails: the twin's grade must pass, and the plant's must fail on the plant's gate alone."""
    found: list[str] = []
    if twin[REQUIRED].conclusion != SUCCESS:
        found.append(f"the twin's {REQUIRED} check concluded {twin[REQUIRED].conclusion}, not {SUCCESS}")
    if head[REQUIRED].conclusion != FAILURE:
        found.append(f"the plant's {REQUIRED} check concluded {head[REQUIRED].conclusion}, not {FAILURE}")
    failed = sorted(gate for gate, check in head.items() if gate != REQUIRED and check.conclusion != SUCCESS)
    if failed != [plant.gate]:
        found.append(f"the plant failed {', '.join(failed) or 'no gate'}, not {plant.gate} alone")
    return found


def _cell(check: Check, head: str) -> str:
    """Return one proof table cell: the check run's conclusion linked to its page, and the head it graded."""
    return f"[{check.conclusion}]({check.url}) on `{head[:12]}`"


def row(plant: Plant, heads: Heads, twin: Mapping[str, Check], head: Mapping[str, Check]) -> str:
    """Return the proof table row: the plant, its gate and reason, and its gate's check run on the twin and the plant."""
    green, red = (found.get(plant.gate, found[REQUIRED]) for found in (twin, head))
    reason = f"| `{plant.name}` | `{plant.gate}` | {plant.reason} |"
    return f"{reason} {_cell(green, heads.twin)} | {_cell(red, heads.plant)} |"


def prove(root: Path, plant: Plant, settings: Settings, ports: Ports) -> tuple[str, list[str]]:
    """Cut and push a plant's twin and plant commits, notice both, wait for both grades and return the table row and
    the proof's problems."""
    heads = commits(root, plant, settings.base)
    push(root, settings.remote, heads, plant.name)
    repository = f"https://github.com/{settings.repository}"
    graded: list[dict[str, Check]] = []
    for head in (heads.twin, heads.plant):
        body = notice(repository, head, heads.base, settings.actor)
        headers = {"X-Hub-Signature-256": signature(settings.secret, body), "Content-Type": "application/json"}
        status = ports.post(settings.notice_url, body, headers)
        if status != 200:
            raise ProofError(f"the lab notice of {head} answered {status}")
    for head in (heads.twin, heads.plant):
        run = run_key(repository, head, heads.base)
        graded.append(settled(ports, settings.repository, head, run, settings.wait_s, settings.poll_s))
    twin, head = graded
    return row(plant, heads, twin, head), problems(plant, twin, head)


def _post(url: str, body: bytes, headers: Mapping[str, str]) -> int:
    """Post a notice and return its status code."""
    return requests.post(url, data=body, headers=dict(headers), timeout=TIMEOUT).status_code


def _get(token: str, path: str) -> Any:
    """Return the JSON of a GitHub API path read with the token; refuse an error answer."""
    response = requests.get(f"{API}/{path}", headers={"Authorization": f"Bearer {token}", **HEADERS}, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def main(argv: Sequence[str], environ: Mapping[str, str], ports: Ports | None = None) -> int:
    """Prove one plant both ways on the deployed lab and print its proof table."""
    parser = argparse.ArgumentParser(prog="python -m scripts.ci_lab grade")
    parser.add_argument("--plant", required=True)
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--repository", default=environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--actor", default=environ.get("GITHUB_TRIGGERING_ACTOR") or environ.get("GITHUB_ACTOR", ""))
    parser.add_argument("--notice-url", default=NOTICE_URL)
    parser.add_argument("--wait", type=float, default=WAIT_S)
    parser.add_argument("--poll", type=float, default=POLL_S)
    args = parser.parse_args(argv)
    secret, token = environ.get(NOTICE_SECRET_ENV, ""), environ.get("GITHUB_TOKEN", "")
    needed = (("--repository", args.repository), ("--actor", args.actor), (NOTICE_SECRET_ENV, secret))
    missing = [name for name, value in (*needed, ("GITHUB_TOKEN", token)) if not value]
    if missing:
        print(f"grade: set {', '.join(missing)}", file=sys.stderr)
        return 2
    settings = Settings(
        args.repository, args.actor, secret.encode(), args.base, args.remote, args.notice_url, args.wait, args.poll
    )
    root = Path.cwd()
    try:
        line, found = prove(
            root, load(root, args.plant), settings, ports or Ports(_post, partial(_get, token), time.sleep)
        )
    except (OSError, KeyError, ProofError, subprocess.CalledProcessError, requests.RequestException) as error:
        print(f"grade: {error}", file=sys.stderr)
        return 2
    print(f"{HEADER}\n{line}")
    for problem in found:
        print(f"grade: {problem}", file=sys.stderr)
    return 1 if found else 0
