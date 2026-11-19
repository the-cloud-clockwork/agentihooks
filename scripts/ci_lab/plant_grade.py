from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.ci_lab import head_notice, plant_proofs
from scripts.ci_lab.digests import GradeInputs, grade_key
from scripts.ci_lab.head_notice import Opener
from scripts.ci_lab.remote.checkout import at
from scripts.ci_lab.webhooks import NOTICE_SECRET_ENV


@dataclass(frozen=True)
class Ports:
    fetch: Callable[[str], Mapping[str, Any]]
    opener: Opener
    sleep: Callable[[float], None]
    clock: Callable[[], float]


def _matching(repository: str, head: str, run: str, ports: Ports) -> list[Mapping[str, Any]]:
    slug = repository.removeprefix("https://github.com/")
    matching: list[Mapping[str, Any]] = []
    for page in range(1, 101):
        path = f"repos/{slug}/commits/{head}/check-runs?filter=all&per_page=100&page={page}"
        rows = ports.fetch(path)["check_runs"]
        matching.extend(
            row for row in rows if row.get("external_id") == run and row["name"].startswith(("lab/", "lab-shadow/"))
        )
        if len(rows) < 100:
            return matching
    raise ValueError(f"grade {run} check history exceeds one hundred pages")


def _read(repository: str, head: str, base: str, ports: Ports, timeout_s: float) -> list[Mapping[str, Any]]:
    run = grade_key(GradeInputs(repository, head, base, "pull_request", base))
    deadline = ports.clock() + timeout_s
    while ports.clock() < deadline:
        rows = _matching(repository, head, run, ports)
        required = [row for row in rows if row["name"].split("/", 1)[1] == "required"]
        if len(required) > 1:
            raise ValueError(f"grade {run} has ambiguous required checks")
        if required and all(row["status"] == "completed" for row in rows):
            return rows
        ports.sleep(min(5, max(0, deadline - ports.clock())))
    raise TimeoutError(f"grade {run} did not finish within {timeout_s} seconds")


def _verdict(rows: list[Mapping[str, Any]], pair: plant_proofs.Pair, planted: bool) -> str:
    checks = {row["name"].split("/", 1)[1]: row for row in rows}
    required = checks.pop("required")
    expected = "failure" if planted else "success"
    failed = {gate for gate, check in checks.items() if check["conclusion"] != "success"}
    if (
        required["conclusion"] != expected
        or pair.gate not in checks
        or failed != ({pair.gate} if planted else set())
        or planted
        and checks[pair.gate]["conclusion"] != "failure"
    ):
        raise ValueError(f"{pair.name} {'plant' if planted else 'twin'} grade has the wrong verdict: {failed}")
    return str(required["html_url"])


def _push(root: Path, pair: plant_proofs.Pair, head: str) -> None:
    branch = f"refs/heads/diffcheck/ci-lab-{pair.name}"
    git = at(root)
    result = git("ls-remote", "--heads", "origin", branch)
    result.check_returncode()
    remote = result.stdout.split()
    if remote and remote[0] in {head, pair.red}:
        return
    git("push", "origin", f"{head}:{branch}").check_returncode()


def prove(
    root: Path,
    name: str,
    base: str,
    repository: str,
    environ: Mapping[str, str],
    ports: Ports,
    timeout_s: float = 900,
) -> dict[str, str]:
    if not all(environ.get(key) for key in (head_notice.URL_ENV, NOTICE_SECRET_ENV, head_notice.ACTOR_ENV)):
        raise ValueError("the lab notice URL, secret and actor must be configured")
    if timeout_s <= 0:
        raise ValueError("the grade timeout must be positive")
    pair = plant_proofs.prepare(root, name, base)
    links: list[str] = []
    for head, planted in ((pair.green, False), (pair.red, True)):
        _push(root, pair, head)
        status = head_notice.send(environ, repository, head, pair.base, opener=ports.opener)
        if status != 202:
            raise ValueError(f"lab head notice was refused: {status}")
        rows = _read(repository, head, pair.base, ports, timeout_s)
        links.append(_verdict(rows, pair, planted))
    return {
        "plant": pair.name,
        "gate": pair.gate,
        "reason": pair.reason,
        "base": pair.base,
        "green": links[0],
        "red": links[1],
    }
