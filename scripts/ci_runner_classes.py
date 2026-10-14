import argparse
import json
from pathlib import Path

from scripts.ci_runner_trust import ACTORS, _compact, selectors

TABLE = ".github/runner-classes.json"
LABELS = {"light": "anton-oss-light", "standard": "anton-oss", "batch": "anton-oss-batch"}
UNROUTED = ("hosted", "dind")
CLASSES = (*UNROUTED, *LABELS)
PINNED_HOSTED = frozenset(
    {
        "gate-protected.yml/grade",
        "pages.yml/build",
        "pages.yml/deploy",
        "publish-pypi.yml/publish",
        "publish-pypi.yml/verify",
        "release.yml/release",
        "runner-trust.yml/actors",
    }
)


def _runner_problem(key: str, cls: str, runner: object, policy: dict) -> str | None:
    if runner == "ubuntu-latest":
        return None
    if cls in UNROUTED:
        return f"{key}: runs-on must be ubuntu-latest for a {cls} job"
    if not isinstance(runner, str) or _compact(runner) not in selectors(policy, LABELS[cls]):
        return f"{key}: runs-on must be ubuntu-latest or the routed {cls} runner"
    return None


def check(workflows: dict[str, dict], table: dict[str, str], policy: dict) -> list[str]:
    problems = [
        f"{file}/{name}: runner classes cannot resolve local workflow {job['uses']}"
        for file, workflow in workflows.items()
        for name, job in (workflow.get("jobs") or {}).items()
        if str(job.get("uses")).startswith("./.github/workflows/") and Path(job["uses"]).name not in workflows
    ]
    jobs = {
        f"{file}/{name}": job
        for file, workflow in workflows.items()
        for name, job in (workflow.get("jobs") or {}).items()
        if not job.get("uses")
    }
    for key, job in jobs.items():
        cls = table.get(key)
        if cls is None:
            problems.append(f"{key}: runner class missing")
        elif cls not in CLASSES:
            problems.append(f"{key}: runner class {cls} is not one of {', '.join(CLASSES)}")
        elif key in PINNED_HOSTED and cls != "hosted":
            problems.append(f"{key}: runner class must be hosted")
        elif problem := _runner_problem(key, cls, job.get("runs-on"), policy):
            problems.append(problem)
    problems += [f"{key}: runner class names no job" for key in table if key not in jobs]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    import yaml

    workflows = {
        path.name: yaml.safe_load(path.read_text()) or {}
        for path in sorted((args.root / ".github/workflows").iterdir())
        if path.suffix in (".yml", ".yaml")
    }
    policy = json.loads(Path(ACTORS).read_text())
    problems = check(workflows, json.loads((args.root / TABLE).read_text()), policy)
    jobs = sum(len(workflow.get("jobs") or {}) for workflow in workflows.values())
    print(f"Runner classes: {len(workflows)} workflows, {jobs} jobs, {len(problems)} problems")
    for problem in problems:
        print(f"::error::{problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
