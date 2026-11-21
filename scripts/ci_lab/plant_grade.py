import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.ci_lab import plant_proofs, plant_publish
from scripts.ci_lab.digests import GradeInputs, grade_key
from scripts.ci_lab.remote.checkout import at


@dataclass(frozen=True)
class Ports:
    fetch: Callable[[str, float], Mapping[str, Any]]
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    push: Callable[[Path, plant_proofs.Pair, str, str], None]


def _fetch(path: str, timeout_s: float) -> Mapping[str, Any]:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=True, timeout=min(30, timeout_s))
    return json.loads(result.stdout)


def _matching(repository: str, head: str, run: str, ports: Ports, deadline: float) -> list[Mapping[str, Any]]:
    slug = repository.removeprefix("https://github.com/")
    matching: list[Mapping[str, Any]] = []
    for page in range(1, 101):
        path = f"repos/{slug}/commits/{head}/check-runs?filter=all&per_page=100&page={page}"
        remaining = deadline - ports.clock()
        if remaining <= 0:
            raise TimeoutError(f"grade {run} expired while reading checks")
        rows = ports.fetch(path, remaining)["check_runs"]
        if ports.clock() >= deadline:
            raise TimeoutError(f"grade {run} expired while reading checks")
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
        rows = _matching(repository, head, run, ports, deadline)
        required = [row for row in rows if row["name"].split("/", 1)[1] == "required"]
        if len(required) > 1:
            raise ValueError(f"grade {run} has ambiguous required checks")
        if required and all(row["status"] == "completed" for row in rows):
            return rows
        ports.sleep(min(5, max(0, deadline - ports.clock())))
    raise TimeoutError(f"grade {run} did not finish within {timeout_s} seconds")


def _verdict(rows: list[Mapping[str, Any]], pair: plant_proofs.Pair, planted: bool) -> str:
    checks = {row["name"].split("/", 1)[1]: row for row in rows}
    if len(checks) != len(rows):
        raise ValueError(f"{pair.name} grade has ambiguous gate checks")
    required = checks.pop("required")
    gate = pair.gate.rpartition("/")[0] or pair.gate
    expected = "failure" if planted else "success"
    failed = {gate for gate, check in checks.items() if check["conclusion"] != "success"}
    if (
        required["conclusion"] != expected
        or gate not in checks
        or failed != ({gate} if planted else set())
        or planted
        and checks[gate]["conclusion"] != "failure"
    ):
        raise ValueError(f"{pair.name} {'plant' if planted else 'twin'} grade has the wrong verdict: {failed}")
    if planted:
        diagnostic = pair.reason.partition(":")[0]
        output = checks[gate].get("output") or {}
        if not re.search(rf"\b{re.escape(diagnostic)}\b", output.get("text") or ""):
            raise ValueError(f"{pair.name} plant grade lacks the expected diagnostic: {diagnostic}")
    return str(required["html_url"])


def prove(
    root: Path,
    name: str,
    base: str,
    repository: str,
    ports: Ports,
    timeout_s: float = 900,
) -> dict[str, str]:
    if timeout_s <= 0:
        raise ValueError("the grade timeout must be positive")
    pair = plant_proofs.prepare(root, name, base)
    links: list[str] = []
    runs: list[str] = []
    for head, planted in ((pair.green, False), (pair.red, True)):
        ports.push(root, pair, head, repository)
        rows = _read(repository, head, pair.base, ports, timeout_s)
        links.append(_verdict(rows, pair, planted))
        runs.append(str(rows[0]["external_id"]))
    return {
        "plant": pair.name,
        "gate": pair.gate,
        "reason": pair.reason,
        "base": pair.base,
        "green": links[0],
        "red": links[1],
        "green_run": runs[0],
        "red_run": runs[1],
    }


def main(argv: Sequence[str], environ: Mapping[str, str], ports: Ports | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.ci_lab grade")
    parser.add_argument(
        "--plant",
        choices=sorted(path.stem for path in (Path.cwd() / plant_proofs.CATALOG).glob("*.json")),
        required=True,
    )
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--repository")
    parser.add_argument("--timeout", type=float, default=900)
    arguments = parser.parse_args(argv)
    root = Path.cwd()
    try:
        repository = arguments.repository
        if repository is None:
            remote = at(root)("remote", "get-url", "origin")
            remote.check_returncode()
            repository = remote.stdout.strip()
        parsed = urlsplit(repository.replace("git@github.com:", "https://github.com/"))
        if parsed.hostname != "github.com":
            raise ValueError("a GitHub repository URL is required")
        canonical = f"https://github.com{parsed.path.rstrip('/').removesuffix('.git')}".lower()
        row = prove(
            root,
            arguments.plant,
            arguments.base,
            canonical,
            ports or Ports(_fetch, time.sleep, time.monotonic, plant_publish.publish),
            arguments.timeout,
        )
        if summary := environ.get("GITHUB_STEP_SUMMARY"):
            with Path(summary).open("a") as output:
                output.write(
                    f"| {row['plant']} | {row['gate']} | {row['reason']} | "
                    f"[green]({row['green']}) ({row['green_run']}) | "
                    f"[red]({row['red']}) ({row['red_run']}) |\n"
                )
        print(json.dumps(row))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
