import argparse
import json
import re
from pathlib import Path

ACTORS = ".github/workflows/trusted-actors.json"
LOADER = "the-cloud-clockwork/agentihooks/.github/workflows/runner-trust.yml@dev"
GUARD = (
    "github.event_name != 'pull_request_target' && "
    "(github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) && "
    "contains(fromJSON(needs.runner-trust.outputs.actors), github.actor) && "
    "contains(fromJSON(needs.runner-trust.outputs.actors), github.triggering_actor) && "
    "(github.actor != fromJSON(needs.runner-trust.outputs.queue_actor) || github.event_name == 'merge_group' || "
    "(github.event_name == 'push' && github.ref == 'refs/heads/dev')) && "
    "(github.triggering_actor != fromJSON(needs.runner-trust.outputs.queue_actor) || github.event_name == 'merge_group' || "
    "(github.event_name == 'push' && github.ref == 'refs/heads/dev'))"
)


def loader_contract() -> dict:
    command = (
        "python3 -I -c 'import json; from pathlib import Path; "
        'p=json.loads(Path(".github/workflows/trusted-actors.json").read_text()); '
        'print("actors=" + json.dumps(p["actors"] + [p["queue_actor"]], separators=(",", ":"))); '
        'print("queue_actor=" + json.dumps(p["queue_actor"]))'
        '\' >> "$GITHUB_OUTPUT"'
    )
    return {
        "on": {
            "workflow_call": {
                "outputs": {
                    name: {"value": "${{ jobs.actors.outputs." + name + " }}"} for name in ("actors", "queue_actor")
                }
            }
        },
        "permissions": {"contents": "read"},
        "jobs": {
            "actors": {
                "runs-on": "ubuntu-latest",
                "timeout-minutes": 2,
                "outputs": {name: "${{ steps.actors.outputs." + name + " }}" for name in ("actors", "queue_actor")},
                "steps": [
                    {
                        "uses": "actions/checkout@v4",
                        "with": {
                            "repository": "the-cloud-clockwork/agentihooks",
                            "ref": "dev",
                            "persist-credentials": False,
                        },
                    },
                    {"name": "Read protected actor approvals", "id": "actors", "run": command},
                ],
            }
        },
    }


def validate_actors(actors: object) -> list[str]:
    if isinstance(actors, dict):
        if set(actors) != {"actors", "queue_actor"} or not isinstance(actors["actors"], list):
            raise ValueError("trusted actors policy requires actors and queue_actor")
        approved = validate_actors(actors["actors"])
        queue = actors["queue_actor"]
        if not isinstance(queue, str) or not re.fullmatch(r"[A-Za-z0-9-]+\[bot\]", queue) or queue in approved:
            raise ValueError("trusted actors queue identity must be a distinct platform bot")
        return approved + [queue]
    if (
        not isinstance(actors, list)
        or len(actors) != 3
        or any(not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", actor) for actor in actors)
        or len(set(actors)) != 3
        or sum(actor.endswith("[bot]") for actor in actors) != 2
    ):
        raise ValueError("trusted actors must contain one operator and two distinct app bots")
    return actors


def _compact(expression: str) -> str:
    return re.sub(r"'(?:''|[^'])*'|\s+", lambda match: match[0] if match[0].startswith("'") else "", expression)


def _guarded(job: dict, jobs: dict) -> bool:
    needs = job.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    loader = jobs.get("runner-trust", {})
    if "runner-trust" not in needs or loader != {"uses": LOADER}:
        return False
    expression = str(job.get("if", "")).strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    guard = _compact(GUARD)
    expression = _compact(expression)
    if expression == guard:
        return True
    if not expression.startswith(guard + "&&"):
        return False
    extra = expression[len(guard) + 2 :]
    if not extra or any(token in extra for token in ("||", "?", "}", "{", '"')):
        return False
    depth = 0
    for token in re.findall(r"'(?:''|[^'])*'|.", extra):
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _hosted(runner: object) -> bool:
    return isinstance(runner, str) and bool(
        re.fullmatch(r"(?:ubuntu|windows|macos)-(?:latest|[0-9]+(?:\.[0-9]+)*)(?:-arm|-large|-xlarge)?", runner)
    )


def _selector(runner: object, policy: object) -> bool:
    if not isinstance(runner, str) or not isinstance(policy, dict):
        return False
    members = json.dumps(policy["actors"], separators=(",", ":"))
    queue = policy["queue_actor"]
    condition = (
        "(github.event_name != 'pull_request_target' && "
        "(github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) && "
        f"contains(fromJSON('{members}'), github.actor) && contains(fromJSON('{members}'), github.triggering_actor) || "
        "(github.event_name == 'merge_group' || github.event_name == 'push' && github.ref == 'refs/heads/dev') && "
        f"github.actor == '{queue}' && github.triggering_actor == '{queue}')"
    )
    return any(
        _compact(runner) == _compact("${{ " + condition + " && '" + pool + "' || 'ubuntu-latest' }}")
        for pool in ("anton-oss", "anton-oss-light", "anton-oss-batch")
    )


def check(workflows: dict[str, dict], actors: object = None) -> list[str]:
    if actors is not None:
        validate_actors(actors)
    problems = []
    if "runner-trust.yml" in workflows:
        loader = dict(workflows["runner-trust.yml"])
        loader.pop("name", None)
        if True in loader:
            loader["on"] = loader.pop(True)
        if loader != loader_contract():
            problems.append("runner-trust.yml: runner trust loader may not override protected actor data")
    for file, workflow in workflows.items():
        events = workflow.get("on", workflow.get(True, {}))
        events = [events] if isinstance(events, str) else events
        jobs = workflow.get("jobs", {})
        for name, job in jobs.items():
            uses = job.get("uses", "")
            if uses == LOADER:
                continue
            if uses.startswith("./.github/workflows/"):
                if Path(uses).name not in workflows:
                    problems.append(f"{file}/{name}: runner trust cannot resolve local workflow {uses}")
                continue
            if not uses and (_hosted(job.get("runs-on")) or _selector(job.get("runs-on"), actors)):
                continue
            if "pull_request_target" in events or not _guarded(job, jobs):
                problems.append(
                    f"{file}/{name}: runner trust requires protected actors, triggering actor, same repository and no target event"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--actors-output", action="store_true")
    args = parser.parse_args(argv)
    policy = json.loads((args.root / ACTORS).read_text())
    actors = validate_actors(policy)
    if args.actors_output:
        print("actors=" + json.dumps(actors, separators=(",", ":")))
        return 0
    import yaml

    workflows = {
        path.name: yaml.safe_load(path.read_text()) or {}
        for path in (args.root / ".github/workflows").iterdir()
        if path.suffix in (".yml", ".yaml")
    }
    problems = check(workflows, policy)
    print(
        f"Runner trust: {len(workflows)} workflows, {sum(len(workflow.get('jobs', {})) for workflow in workflows.values())} jobs, {len(problems)} problems"
    )
    for problem in problems:
        print(f"::error::{problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
