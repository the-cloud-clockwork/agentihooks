import argparse
import json
import subprocess
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from scripts import ci_branch_lanes, ci_reuse, ci_runner_trust, ci_wiring

GATE = ci_wiring.GATE
WORKFLOWS = ".github/workflows"
SHALLOW_PREFLIGHT_CHECKOUT = {
    "fetch-depth": 1,
    "sparse-checkout": (
        "/.github/workflows/\n/scripts/__init__.py\n/scripts/gh_config.py\n/scripts/ci_branch_lanes.py\n"
    ),
    "sparse-checkout-cone-mode": False,
}
FULL_PREFLIGHT_CHECKOUT = {"fetch-depth": 0}
PREFLIGHT_FETCH = 'git fetch --no-tags origin "$GITHUB_SHA"'
SHALLOW_PREFLIGHT_FETCH = 'git fetch --no-tags --depth 1 origin "$GITHUB_SHA"'
EQUIVALENCE = ("equivalence.yml", "ledger-equivalence")
EQUIVALENCE_WORDS = ("GRADER", "GITHUB_ENV", "GITHUB_PATH", "equivalence.sh")
EQUIVALENCE_TUNABLE = ("steps", "timeout-minutes")
UNUSED_SELECT_OUTPUTS = ("files_1", "files_2", "files_3")


def _gates(workflows: dict[str, dict]) -> list[tuple[str, str, dict]]:
    return [
        (file, job_id, job)
        for file, workflow in workflows.items()
        for job_id, job in (workflow.get("jobs") or {}).items()
        if job.get("name") == GATE
    ]


def _protected_config(base: dict, head: dict) -> dict:
    return {
        section: {key: entry for key, entry in base.get(section, {}).items() if key in head.get(section, {})}
        for section in ("not_gates", "outside_gate")
    }


def runners(policy: object = None) -> list[str]:
    anton = (
        [ci_runner_trust.selector(policy, "anton-oss-light", trusted_reruns=reruns) for reruns in (False, True)]
        if isinstance(policy, dict)
        else []
    )
    return ["ubuntu-latest", *anton]


def reuse_jobs(policy: object = None) -> list[dict]:
    jobs = []
    for factory in (ci_reuse.reuse_job, ci_reuse.paged_reuse_job):
        for runner in runners(policy):
            job = factory()
            if runner != job["runs-on"]:
                job["runs-on"] = runner
                [install] = [step for step in job["steps"] if step.get("name") == "Install metadata dependencies"]
                install["env"] = {"PYTHONNOUSERSITE": "1"}
            jobs.append(job)
            jobs.append({**job, "if": "github.event_name != 'merge_group' && github.event_name != 'pull_request'"})
    return jobs


def _aggregator_problems(
    base: tuple[str, str, dict], head: tuple[str, str, dict], policy: object = None, retired: frozenset = frozenset()
) -> list[str]:
    (base_file, base_id, base_job), (head_file, head_id, head_job) = base, head
    if (base_file, base_id) != (head_file, head_id):
        return [f"The head moves {GATE} from {base_file}/{base_id} to {head_file}/{head_id}."]
    problems = [
        f"The head's {GATE} drops the base need {need}."
        for need in sorted(ci_wiring.needs_of(base_job) - ci_wiring.needs_of(head_job) - retired)
    ]
    fields = sorted((set(base_job) | set(head_job)) - {"needs"})
    postgres = any(ci_reuse.POSTGRES_REQUIRED[1] in str(step.get("run")) for step in base_job.get("steps") or [])
    requirements = (ci_reuse.postgres_required,) if postgres else (list, ci_reuse.postgres_required)
    return problems + [
        f"The head's {GATE} changes its {field}; the protected branch grades with its own."
        for field in fields
        if (
            base_job.get(field) != head_job.get(field)
            or field == "steps"
            and base_id == "gate-required"
            and "docs" in ci_wiring.needs_of(head_job)
        )
        and not (
            field == "steps"
            and base_id == "gate-required"
            and head_job.get("steps")
            in [
                diagnostic(
                    forward(
                        finish(
                            rerun(
                                shape(
                                    required(
                                        ci_reuse.gate_steps(
                                            **form,
                                            summary=summary,
                                            docs="docs" in ci_wiring.needs_of(head_job),
                                            watcher_loss=watcher_loss,
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
                for diagnostic, forward in (
                    (list, list),
                    (ci_reuse.selection_diagnostic, list),
                    (ci_reuse.selection_diagnostic, ci_reuse.forwarded_stats),
                )
                for form in (
                    {},
                    {"selected": True},
                    {"queue_tier": True},
                    {"queue_tier": True, "promotion": True},
                    {"queue_tier": True, "consolidated": True},
                    {"queue_tier": True, "promotion": True, "consolidated": True},
                )
                for summary in (False, True)
                for watcher_loss in (False, True)
                for required in requirements
                for rerun in (list, ci_reuse.lost_rerun)
                for shape in (list, ci_reuse.inline_mutation)
                for finish in (list, ci_reuse.pipefail_summary)
            ]
        )
        and not (field == "runs-on" and head_job.get(field) in runners(policy))
    ]


def _reuse_problems(base: dict, head: dict, policy: object = None) -> list[str]:
    previous = base.get("reuse")
    candidate = head.get("reuse")
    if candidate is None:
        return ["The head removes the protected reuse job."] if previous is not None else []
    if candidate != previous and candidate not in reuse_jobs(policy):
        return ["The head changes the protected reuse job; the protected branch grades with its own."]
    return []


def _duration_problems(base: dict, head: dict) -> list[str]:
    if (
        head["jobs"].get("reuse", {}).get("if")
        != "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
    ):
        return []
    previous = base["jobs"].get("durations", base["jobs"].get("select", {}))
    producer = head["jobs"].get("durations", head["jobs"].get("select", {}))
    prefix = ci_reuse.duration_provenance_steps()
    problems = []
    if "durations" in base["jobs"] and "durations" not in head["jobs"]:
        selection = base["jobs"].get("select", {})
        if not selection:
            problems.append("The head moves durations provenance without existing selection admission.")
        previous = previous | {
            "outputs": previous.get("outputs", {}) | selection.get("outputs", {}),
            "timeout-minutes": selection.get("timeout-minutes"),
        }
    bounded_prefix = [
        prefix[0] | {"env": {"GIT_HTTP_LOW_SPEED_LIMIT": "1000", "GIT_HTTP_LOW_SPEED_TIME": "60"}},
        *prefix[1:],
    ]
    if producer.get("steps", [])[: len(prefix)] not in (prefix, bounded_prefix):
        problems.append("The head skips reuse without protected durations provenance.")
    mutable = {"steps", "if", "runs-on"}
    outputs = previous.get("outputs") or {}
    trimmed = {key: value for key, value in outputs.items() if key not in UNUSED_SELECT_OUTPUTS}
    problems += [
        f"The head changes the durations provenance context {field}."
        for field in sorted((set(previous) | set(producer)) - mutable)
        if previous.get(field) != producer.get(field)
        and not (
            field == "outputs"
            and producer is head["jobs"].get("select")
            and set(UNUSED_SELECT_OUTPUTS).issubset(outputs)
            and producer.get(field) == trimmed
        )
    ]
    problems += [
        f"The head changes the global provenance context {field}."
        for field in ("env", "defaults")
        if base.get(field) != head.get(field)
    ]
    return problems


def grade(
    base_workflows: dict[str, dict],
    base_config: dict,
    head_workflows: dict[str, dict],
    head_config: dict,
    today: date,
    policy: object = None,
) -> list[str]:
    from scripts import ci_gate_queue

    try:
        base_workflows = ci_gate_queue.normalize(base_workflows)
        head_workflows = ci_gate_queue.normalize(head_workflows)
    except ValueError as error:
        return [str(error)]
    base_gates, head_gates = _gates(base_workflows), _gates(head_workflows)
    if len(base_gates) != 1:
        return [f"The base holds {len(base_gates)} jobs named {GATE}; nothing protected can grade."]
    if len(head_gates) != 1:
        return [f"The head holds {len(head_gates)} jobs named {GATE}; exactly one must be."]
    file = base_gates[0][0]
    head_jobs = (head_workflows.get(file) or {}).get("jobs") or {}
    retired = frozenset(
        job_id
        for job_id, job in base_workflows[file]["jobs"].items()
        if job.get("if") is False and job_id not in head_jobs
    )
    duration_problems = _duration_problems(base_workflows[file], head_workflows.get(file, {"jobs": {}}))
    if (
        "durations" in base_workflows[file]["jobs"]
        and "durations" not in head_jobs
        and head_jobs.get("reuse", {}).get("if")
        == "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
        and not duration_problems
    ):
        retired = retired | {"durations"}
    problems = _aggregator_problems(base_gates[0], head_gates[0], policy, retired)
    problems += _reuse_problems(base_workflows[file]["jobs"], head_jobs, policy)
    problems += duration_problems
    config = _protected_config(base_config, head_config)
    summary = head_jobs.get("test-summary")
    if summary is not None:
        canonical = [
            {
                "needs": ["unit", "unit-selected"],
                "if": "${{ always() }}",
                "runs-on": runner,
                "timeout-minutes": 5,
                "steps": finish(ci_reuse.summary_steps()),
            }
            for runner in runners(policy)
            for finish in (list, ci_reuse.pipefail_summary)
        ]
        if summary not in canonical:
            problems.append("The test summary must use the reviewed reporting job.")
        else:
            key = f"{file}/test-summary"
            if key in head_config.get("not_gates", {}):
                config["not_gates"].setdefault(key, head_config["not_gates"][key])
    return problems + ci_wiring.check(head_workflows, config, today)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def load(root: Path, rev: str) -> tuple[dict[str, dict], dict]:
    tracked = _git(root, "ls-tree", "-r", "-z", "--name-only", rev).split("\0")
    workflows = {
        Path(path).name: yaml.safe_load(_git(root, "show", f"{rev}:{path}")) or {}
        for path in tracked
        if Path(path).parent == Path(WORKFLOWS) and path.endswith((".yml", ".yaml"))
    }
    config = json.loads(_git(root, "show", f"{rev}:{ci_wiring.CONFIG}")) if ci_wiring.CONFIG in tracked else {}
    return workflows, config


def _preflight_problems(base_workflows: dict[str, dict], head_workflows: dict[str, dict]) -> list[str]:
    baseline = base_workflows.get("branch-lanes.yml")
    if baseline is None:
        return []
    expanded = deepcopy(baseline)
    events = expanded[True] if True in expanded else expanded["on"]
    ignored = (events.get("push") or {}).get("branches-ignore", [])
    if "diffcheck/**" in ignored:
        ignored.remove("diffcheck/**")
    if head_workflows.get("branch-lanes.yml") in (
        baseline,
        expanded,
        *_checkout_forms(baseline),
        *_checkout_forms(expanded),
    ):
        return []
    return ["The head changes or removes the trusted branch lane preflight."]


def _checkout_forms(preflight: dict) -> list[dict]:
    forms = []
    for checkout, fetch in (
        (SHALLOW_PREFLIGHT_CHECKOUT, SHALLOW_PREFLIGHT_FETCH),
        (FULL_PREFLIGHT_CHECKOUT, PREFLIGHT_FETCH),
    ):
        form = deepcopy(preflight)
        steps = ((form.get("jobs") or {}).get("grade") or {}).get("steps") or [{}]
        settings = steps[0].get("with", {})
        for key in SHALLOW_PREFLIGHT_CHECKOUT:
            settings.pop(key, None)
        settings.update(checkout)
        for step in steps:
            if step.get("run") in (PREFLIGHT_FETCH, SHALLOW_PREFLIGHT_FETCH):
                step["run"] = fetch
        forms.append(form)
    return forms


def _equivalence_problems(base_workflows: dict[str, dict], head_workflows: dict[str, dict]) -> list[str]:
    file, job_id = EQUIVALENCE
    base, head = base_workflows.get(file) or {}, head_workflows.get(file) or {}
    base_job, head_job = ((workflow.get("jobs") or {}).get(job_id) or {} for workflow in (base, head))
    base_steps, head_steps = base_job.get("steps") or [], head_job.get("steps") or []
    graded = [index for index, step in enumerate(base_steps) if "$GRADER" in str(step.get("run"))]
    if not graded:
        return []
    end = graded[-1] + 1
    if (
        head_steps[:end] != base_steps[:end]
        or any(word in json.dumps(step) for step in head_steps[end:] for word in EQUIVALENCE_WORDS)
        or {key: value for key, value in head.items() if key != "jobs"}
        != {key: value for key, value in base.items() if key != "jobs"}
        or {key: value for key, value in head_job.items() if key not in EQUIVALENCE_TUNABLE}
        != {key: value for key, value in base_job.items() if key not in EQUIVALENCE_TUNABLE}
    ):
        return ["The head changes the ledger equivalence grading; the protected branch grades with its own."]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", default="HEAD")
    args = parser.parse_args(argv)
    root = Path(args.root)
    merge_base = _git(root, "merge-base", args.base, args.head).strip()
    base_workflows, branched_config = load(root, merge_base)
    tip_workflows, tip_config = load(root, args.base)
    base_config = {
        section: {**branched_config.get(section, {}), **tip_config.get(section, {})}
        for section in ("not_gates", "outside_gate")
    }
    head_workflows, head_config = load(root, args.head)
    actors = None
    if ci_runner_trust.ACTORS in _git(root, "ls-tree", "-r", "--name-only", args.base).splitlines():
        actors = json.loads(_git(root, "show", f"{args.base}:{ci_runner_trust.ACTORS}"))
        ci_runner_trust.validate_actors(actors)
    problems = grade(base_workflows, base_config, head_workflows, head_config, datetime.now(UTC).date(), actors)
    preflight = _preflight_problems(tip_workflows, head_workflows)
    governed = {"branch-lanes.yml"} if "branch-lanes.yml" in tip_workflows and not preflight else set()
    problems += preflight
    problems += _equivalence_problems(tip_workflows, head_workflows)
    problems += [
        f"{file}: {problem}"
        for file, workflow in head_workflows.items()
        if file not in governed and workflow != base_workflows.get(file)
        for problem in ci_branch_lanes.problems(workflow, base_workflows.get(file))
    ]
    if actors is not None:
        problems += ci_runner_trust.check(head_workflows, actors)
    print(f"graded {args.head[:12]} against merge base {merge_base[:12]}: {len(problems)} protected gate problems")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
