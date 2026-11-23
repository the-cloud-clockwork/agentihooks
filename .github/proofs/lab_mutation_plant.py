"""Offline in process proof of the lab mutation plant kind.

Three heads are cut from one dev commit: a clean twin that adds a function with an exact test, a plant that adds the
same function with a weakened test, and a control that changes no measured Python line. Each is turned into a push
grade request and expanded by the lab's own MirrorExpander against the dependency manifest of the dev push run, and
each mutation unit runs through the lab's own mutation executor in its bubblewrap sandbox. No queue, budget, verdict
store or deployed scheduler takes part. Run it from a folder outside the checkout:

    python -I lab_mutation_plant.py --repo CHECKOUT --base SHA --work DIR --out DIR
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib import import_module, metadata
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

SOURCE = "scripts/term_colors.py"
TESTS = "tests/test_term_colors.py"
CONTROL = "README.md"
ANCHOR = 'BLUE = "34"\n'
EXPORTS = '__all__ = ["BLUE", "YELLOW", "RED", "enabled", "paint", "blue", "yellow", "red", "bright"]\n\n'
FUNCTION = "\n\ndef bright(color: str) -> str:\n    return str(int(color) + 60)\n"
EXACT = '\n\ndef test_bright_shifts_a_colour_to_its_bright_code():\n    assert colors.bright("31") == "91"\n'
WEAK = '\n\ndef test_bright_shifts_a_colour_to_its_bright_code():\n    assert colors.bright("31")\n'
PLANTED = "scripts.term_colors.x_bright__mutmut_"
PINNED = ("libcst", "mutmut", "pytest")
GRADED = ("twin", "plant")
MODULES = (
    "digests",
    "executors",
    "expand_tests",
    "intake",
    "manifests",
    "objects",
    "outcomes",
    "run_mutation",
    "sandbox",
    "scheduler",
    "units",
    "webhooks",
    "worker",
    "worktrees",
)
IDENTITY = {
    "GIT_AUTHOR_NAME": "CI lab plant",
    "GIT_AUTHOR_EMAIL": "ci-lab@example.invalid",
    "GIT_COMMITTER_NAME": "CI lab plant",
    "GIT_COMMITTER_EMAIL": "ci-lab@example.invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
}
LOG_TAIL = 200
BUDGET_S = 1800.0
CAP = 1
BEGAN = time.monotonic()
PACE = 100
SLOW_S = 15.0


def say(message: str) -> None:
    """Print one progress line with the seconds since the driver started."""
    print(f"[{time.monotonic() - BEGAN:8.1f} s] {message}", flush=True)


def timed(owner: Any, name: str, timings: list[dict[str, Any]]) -> None:
    """Make owner.name report when each call begins and how long it took, and keep that time; the call itself and
    what it returns or raises stay as they are."""
    inner = getattr(owner, name)

    def reported(*args: Any, **kwargs: Any) -> Any:
        began = time.monotonic()
        say(f"{name} begins")
        try:
            return inner(*args, **kwargs)
        finally:
            seconds = round(time.monotonic() - began, 1)
            timings.append({"call": name, "seconds": seconds})
            say(f"{name} took {seconds} s")

    setattr(owner, name, reported)


def paced(owner: Any, name: str) -> None:
    """Make owner.name report every hundredth call with the seconds spent in it so far, and each call slower than
    fifteen seconds with its arguments; the call itself stays as it is."""
    inner = getattr(owner, name)
    seen = {"calls": 0, "seconds": 0.0}

    def reported(*args: Any, **kwargs: Any) -> Any:
        began = time.monotonic()
        try:
            return inner(*args, **kwargs)
        finally:
            spent = time.monotonic() - began
            seen["calls"] += 1
            seen["seconds"] += spent
            if spent > SLOW_S:
                say(f"{name} took {spent:.1f} s for {args[1:]}")
            if seen["calls"] % PACE == 0:
                say(f"{name} called {seen['calls']} times in {seen['seconds']:.1f} s")

    setattr(owner, name, reported)


@dataclass
class Run:
    """One proof run: the lab modules of the checkout, its folders, its settings and the artifact it writes."""

    lab: SimpleNamespace
    repo: Path
    work: Path
    out: Path
    repository: str
    proof: dict[str, Any]
    requests: dict[str, Any] = field(default_factory=dict[str, Any])
    plans: dict[str, dict[str, list[Any]]] = field(default_factory=dict[str, dict[str, list[Any]]])
    graders: dict[str, str] = field(default_factory=dict[str, str])
    mutation_only: bool = False

    def write(self) -> None:
        """Store the artifact as it stands."""
        (self.out / "proof.json").write_text(json.dumps(self.proof, indent=2, sort_keys=True) + "\n")


class EnvToken:
    """The workflow token, read from the environment at each request."""

    def get_token(self) -> str:
        """Return the token the workflow gave this job."""
        return os.environ["GITHUB_TOKEN"]


def git(root: Path, *arguments: str, data: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    """Run git in root and return what it printed."""
    done = subprocess.run(
        ["git", "-C", str(root), *arguments],
        input=data,
        env={**os.environ, **(environ or {})},
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def commit(repo: Path, base: str, edits: Mapping[str, str], message: str, work: Path) -> str:
    """Return a child commit of base holding the edited files, built on a private index with a fixed identity."""
    with TemporaryDirectory(dir=work) as temporary:
        environ = {**IDENTITY, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        git(repo, "read-tree", base, environ=environ)
        for path, source in edits.items():
            mode = git(repo, "ls-tree", base, "--", path).split()[0]
            blob = git(repo, "hash-object", "-w", "--stdin", data=source).strip()
            git(repo, "update-index", "--cacheinfo", f"{mode},{blob},{path}", environ=environ)
        tree = git(repo, "write-tree", environ=environ).strip()
    return git(repo, "commit-tree", tree, "-p", base, data=f"{message}\n", environ=IDENTITY).strip()


def heads_of(repo: Path, base: str, work: Path) -> dict[str, str]:
    """Cut the twin, the plant and the control from base."""
    source = git(repo, "show", f"{base}:{SOURCE}")
    tests = git(repo, "show", f"{base}:{TESTS}")
    if source.count(ANCHOR) != 1 or "def bright(" in source:
        raise ValueError(f"{SOURCE} at {base} does not take the plant edit")
    planted = source.replace(ANCHOR, EXPORTS + ANCHOR) + FUNCTION
    readme = git(repo, "show", f"{base}:{CONTROL}")
    return {
        "twin": commit(repo, base, {SOURCE: planted, TESTS: tests + EXACT}, "Lab mutation twin", work),
        "plant": commit(repo, base, {SOURCE: planted, TESTS: tests + WEAK}, "Lab mutation plant", work),
        "control": commit(repo, base, {CONTROL: readme + "\n"}, "Lab mutation control", work),
    }


def mirror_of(repo: Path, node: Path, commits: Mapping[str, str]) -> None:
    """Make node a bare mirror that borrows the checkout's objects and names each commit with a branch."""
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    subprocess.run(["git", "init", "--quiet", "--bare", str(node)], check=True)
    (node / "objects" / "info" / "alternates").write_text(f"{common / 'objects'}\n")
    for name, sha in commits.items():
        git(node, "update-ref", f"refs/heads/proof/{name}", sha)


def tail(path: Path) -> str:
    """Return the last lines of a log file, empty when it does not exist."""
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(errors="replace").splitlines()[-LOG_TAIL:])


def versions(python: Path) -> dict[str, str]:
    """Return the version of each pinned mutation dependency the interpreter imports."""
    script = (
        f"import json, importlib.metadata as m; print(json.dumps({{name: m.version(name) for name in {PINNED!r}}}))"
    )
    done = subprocess.run([str(python), "-I", "-c", script], capture_output=True, text=True, check=True)
    found: dict[str, str] = json.loads(done.stdout)
    return found


def lab_of(repo: Path) -> SimpleNamespace:
    """Import the lab modules of the checkout, put first on the import path."""
    sys.path.insert(0, str(repo))
    return SimpleNamespace(**{name: import_module(f"scripts.ci_lab.{name}") for name in MODULES})


def expander_of(run: Run, node: Path, manifest: str) -> Any:
    """Return the lab's mirror expander over node, reading manifests through the lab's own fetch of the dev push run,
    after storing a packed manifest file when one is given."""
    lab = run.lab
    objects = lab.objects.LocalObjects(run.work / "objects")
    if manifest:
        packed = Path(manifest).read_bytes()
        slug = run.repository.removeprefix("https://github.com/")
        objects.write_once(f"{lab.objects.manifest_folder(slug)}{json.loads(packed)['commit']}.json", packed)
    fetcher = lab.manifests.Fetcher(lambda slug: lab.manifests.Actions(slug, auth=EnvToken()))
    stored = lab.scheduler.StoredManifests(objects, fetcher)
    toolchain = lab.digests.Toolchain(sys.version.split()[0], {}, "", "", "", {})
    return lab.scheduler.MirrorExpander(node, run.work / "stores", toolchain, stored, lambda kind, key: None)


def requested(run: Run, name: str, head: str, base: str, trusted: frozenset[str]) -> Any:
    """Return the promotion grade request the lab makes of a push of head over base by a trusted actor."""
    payload = {
        "ref": f"refs/heads/proof/{name}",
        "before": base,
        "after": head,
        "deleted": False,
        "repository": {"html_url": run.repository},
        "sender": {"login": sorted(trusted)[0]},
    }
    [event] = run.lab.webhooks.head_events("push", payload)
    request = run.lab.intake.admit(event, trusted)
    if request is None or request.tier != run.lab.units.Priority.PROMOTION:
        raise ValueError(f"the push of {name} did not become a promotion grade request: {request}")
    return request


def inputs_of(expander: Any, request: Any) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Return the dependency manifest and the head's file contents an expansion of the request reads."""
    mirror = expander._mirror(request.repo)
    for sha in (request.head, request.base):
        mirror.fetch(sha)
    manifest = expander._manifest(mirror, request)
    return manifest, expander._contents(mirror, expander._files(mirror, request.head))


def mutation_gates(expander: Any, request: Any, record: dict[str, Any], path: str) -> tuple[dict[str, list[Any]], str]:
    """Return only the mutation gates of the request with its head as grader, noting the path taken in the record."""
    record["path"] = path
    manifest, contents = inputs_of(expander, request)
    record["manifest_commit"] = manifest["commit"]
    return expander._mutation(expander._mirror(request.repo), request, manifest, contents), request.head


def gates_of(
    expander: Any, request: Any, record: dict[str, Any], mutation_only: bool
) -> tuple[dict[str, list[Any]], str]:
    """Return the gates of the request's whole plan with its grader, or only its mutation gates when asked or when
    the whole expansion raises, noting the path taken and the error in the record."""
    if mutation_only:
        return mutation_gates(expander, request, record, "MirrorExpander._mutation, asked")
    try:
        plan = expander.expand(request)
    except Exception:
        record["error"] = traceback.format_exc()
        return mutation_gates(expander, request, record, "MirrorExpander._mutation")
    record["path"] = "MirrorExpander.expand"
    record["manifest_commit"] = plan.manifest
    return {job: list(units) for job, units in plan.gates.items()}, plan.grader


def key_inputs(run: Run, expander: Any, request: Any) -> dict[str, Any]:
    """Return what the mutant unit keys of the request take from the tests: how many the manifest lists, how many
    hashes of unlisted test modules enter every key and where the plant's test module falls."""
    try:
        manifest, contents = inputs_of(expander, request)
        unlisted = run.lab.expand_tests.unlisted_test_hashes(manifest, contents)
        listed = {test.partition("::")[0] for test in manifest["tests"]}
    except Exception:
        return {"error": traceback.format_exc()}
    return {
        "manifest_tests": len(manifest["tests"]),
        "unlisted_hashes": len(unlisted),
        "plant_test_module_listed": TESTS in listed,
        "plant_test_module_in_unlisted": TESTS in unlisted,
    }


def expand(run: Run, expander: Any, name: str, request: Any) -> None:
    """Expand one head and keep its mutation units, its grader and the record of its expansion."""
    scheduler = run.lab.scheduler
    record: dict[str, Any] = {"tier": int(request.tier), "error": ""}
    began = time.monotonic()
    say(f"expansion of the {name} begins")
    gates, run.graders[name] = gates_of(expander, request, record, run.mutation_only)
    record["seconds"] = round(time.monotonic() - began, 1)
    say(f"expansion of the {name} took {record['seconds']} s by {record['path']}")
    record["jobs"] = {job: len(units) for job, units in gates.items()}
    mutation = {job: gates.get(job, []) for job in (scheduler.STATS_JOB, scheduler.MUTANT_JOB)}
    record["mutation"] = [
        {
            "job": job,
            "kind": unit.kind,
            "key": unit.key,
            "members": list(unit.members),
            "traced": json.loads(unit.manifest) if unit.manifest else None,
        }
        for job, units in mutation.items()
        for unit in units
    ]
    record["key_inputs"] = key_inputs(run, expander, request)
    objects = run.lab.objects.LocalObjects(run.work / "objects")
    record["manifests_kept"] = scheduler.keep_manifests(
        objects, [unit for units in mutation.values() for unit in units]
    )
    run.plans[name] = mutation
    run.proof["expansion"][name] = record
    run.write()


def planted_mutants(run: Run, head: str) -> list[str]:
    """Return the name of every mutant mutmut makes of the planted function at head."""
    with TemporaryDirectory(dir=run.work) as scratch:
        target = Path(scratch) / SOURCE
        target.parent.mkdir(parents=True)
        source = git(run.repo, "show", f"{head}:{SOURCE}")
        target.write_text(source)
        every = set(range(1, len(source.splitlines()) + 1))
        names: list[str] = run.lab.run_mutation.trampoline(Path(scratch), SOURCE, every)
    return sorted(name for name in names if name.startswith(PLANTED))


def pod_of(run: Run, node: Path, timeout: float) -> tuple[Any, Any]:
    """Return a worker pod over node with a cold stats cache and the mutation settings of the lab chart."""
    executors, root = run.lab.executors, run.work / "pod"
    mirror = run.lab.worktrees.Mirror(node, root / "store", str(node))
    trees = executors.Trees(run.lab.worktrees.Worktrees(mirror, root / "trees", executors.TREES))
    ports = run.lab.sandbox.PortAllocator(executors.FIRST_PORT, executors.LAST_PORT, executors.PORT_WIDTH)
    pod = executors.Pod(trees, root, ports, executors.WHITELIST, timeout)
    for folder in (pod.runs, pod.venvs):
        folder.mkdir(parents=True, exist_ok=True)
    return pod, executors.Mutation(root / "mutation-cache", root / "killers", BUDGET_S, CAP)


def graded(run: Run, pod: Any, settings: Any, lease: Any, label: str) -> dict[str, Any]:
    """Run one leased mutation unit through the lab's executor and return its row of the artifact."""
    row: dict[str, Any] = {"kind": lease.unit.kind, "key": lease.unit.key}
    began = time.monotonic()
    try:
        finished = run.lab.executors.mutation(pod, settings, lease)
        report = json.loads(finished.report.read_text())
        row["outcome"] = finished.outcome
        row["rows"] = report.get("rows", [])
        row["report"] = {key: value for key, value in report.items() if key not in ("rows", "stats")}
        row["stats_records"] = len(report.get("stats", []))
        log = run.out / "logs" / f"{label}.log"
        log.write_text(tail(finished.log))
        row["log"] = log.name
    except Exception:
        row["outcome"] = "raised"
        row["error"] = traceback.format_exc()
    row["seconds"] = round(time.monotonic() - began, 1)
    return row


def grade(run: Run, pod: Any, settings: Any, name: str, head: str) -> None:
    """Grade every mutation unit of one head, its stats units first, and keep the head's verdict."""
    scheduler, outcomes = run.lab.scheduler, []
    objects = run.lab.objects.LocalObjects(run.work / "objects")
    for job in (scheduler.STATS_JOB, scheduler.MUTANT_JOB):
        for index, unit in enumerate(run.plans[name][job]):
            request = run.requests[name]
            leased = replace(unit, manifest=scheduler.stored_manifest(objects, unit))
            lease = run.lab.worker.Lease(
                scheduler.job_id(head, unit), "offline", head, leased, 1, 0, request.base, run.graders[name]
            )
            say(f"{job} unit {index} of the {name} begins: {unit.kind}, {len(unit.members)} members")
            row = {"head": name, "job": job, **graded(run, pod, settings, lease, f"{name}-{job}-{index}")}
            say(f"{job} unit {index} of the {name} ended {row['outcome']} in {row['seconds']} s")
            outcomes.append(row["outcome"])
            run.proof["units"].append(row)
            run.write()
    passed = bool(outcomes) and all(outcome == run.lab.outcomes.PASSED for outcome in outcomes)
    run.proof["verdicts"][name] = "green" if passed else "red"


def checks_of(run: Run, expected: list[str]) -> dict[str, bool]:
    """Return each claim of the proof with whether the run holds it."""
    job = run.lab.scheduler.MUTANT_JOB
    members = {name: sorted(member for unit in run.plans[name][job] for member in unit.members) for name in GRADED}
    keys = {name: sorted(unit.key for unit in run.plans[name][job]) for name in GRADED}
    rows = {
        name: [row for unit in run.proof["units"] if unit["head"] == name for row in unit.get("rows", [])]
        for name in GRADED
    }
    verdicts = run.proof["verdicts"]
    return {
        "every planted mutant of the twin is in a mutant unit": bool(expected) and members["twin"] == expected,
        "every planted mutant of the plant is in a mutant unit": bool(expected) and members["plant"] == expected,
        "the control head expands no mutation unit": not any(run.plans["control"].values()),
        "the twin is graded green": verdicts.get("twin") == "green",
        "every mutant of the twin is killed": bool(rows["twin"])
        and all(row["status"] == "killed" for row in rows["twin"]),
        "the plant is graded red": verdicts.get("plant") == "red",
        "the plant has a surviving mutant": any(row["status"] == "survived" for row in rows["plant"]),
        "the plant has only killed and surviving mutants": bool(rows["plant"])
        and all(row["status"] in ("killed", "survived") for row in rows["plant"]),
        "the twin and the plant have different mutant unit keys": bool(keys["twin"]) and keys["twin"] != keys["plant"],
    }


def table(run: Run, heads: Mapping[str, str]) -> str:
    """Return the proof table and the list of checks as Markdown."""
    lines = ["| Kind | Head | Commit | Mutation units | Verdict |", "|---|---|---|---|---|"]
    for name, head in heads.items():
        count = sum(len(units) for units in run.plans[name].values())
        verdict = run.proof["verdicts"].get(name, "not graded")
        lines.append(f"| mutation, offline in process | {name} | `{head}` | {count} | {verdict} |")
    lines.append("")
    lines.extend(f"- {'pass' if held else 'FAIL'}: {check}" for check, held in run.proof["checks"].items())
    return "\n".join(lines) + "\n"


def parsed(argv: Sequence[str]) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(prog="lab_mutation_plant")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repository", default="https://github.com/the-cloud-clockwork/agentihooks")
    parser.add_argument("--timeout", type=float, default=0.0)
    parser.add_argument("--stats-scope", choices=("every",), default="every")
    parser.add_argument("--expand-only", action="store_true")
    parser.add_argument("--mutation-only", action="store_true")
    parser.add_argument("--manifest", default="")
    return parser.parse_args(argv)


def opened(arguments: argparse.Namespace) -> tuple[Run, str, dict[str, str], float]:
    """Return the run with its empty artifact, the base commit, the three heads and the unit timeout."""
    repo, work, out = (Path(value).resolve() for value in (arguments.repo, arguments.work, arguments.out))
    for folder in (work, out, out / "logs"):
        folder.mkdir(parents=True, exist_ok=True)
    lab = lab_of(repo)
    timeout = arguments.timeout or lab.executors.UNIT_TIMEOUT_S
    base = git(repo, "rev-parse", "--verify", f"{arguments.base}^{{commit}}").strip()
    heads = heads_of(repo, base, work)
    proof: dict[str, Any] = {
        "mode": "offline/in-process",
        "repository": arguments.repository,
        "repo_sha": git(repo, "rev-parse", "HEAD").strip(),
        "base": base,
        "heads": heads,
        "unit_timeout_seconds": timeout,
        "stats_scope": "the test files each mutant unit's manifest names",
        "expansion": {},
        "units": [],
        "timings": [],
        "verdicts": {},
        "checks": {},
        "dependencies": {"driver": {name: metadata.version(name) for name in PINNED}},
    }
    run = Run(lab, repo, work, out, arguments.repository, proof, mutation_only=arguments.mutation_only)
    return run, base, heads, timeout


def finish(run: Run, pod: Any, heads: Mapping[str, str], expected: list[str]) -> int:
    """Record the control verdict, the unit venv's dependency versions and the checks, write the table and return 0
    only when every check holds."""
    run.proof["verdicts"]["control"] = "mutation units" if any(run.plans["control"].values()) else "no mutation unit"
    for venv in sorted(pod.venvs.glob("*/bin/python")):
        run.proof["dependencies"]["unit"] = versions(venv)
    run.proof["checks"] = checks_of(run, expected)
    run.write()
    summary = table(run, heads)
    (run.out / "summary.md").write_text(summary)
    print(summary)
    shutil.rmtree(run.work / "pod", ignore_errors=True)
    return 0 if all(run.proof["checks"].values()) else 1


def main(argv: Sequence[str]) -> int:
    """Expand and grade the three heads, write the proof artifact and return 0 only when every check holds."""
    arguments = parsed(argv)
    run, base, heads, timeout = opened(arguments)
    node = run.work / "node.git"
    mirror_of(run.repo, node, {"base": base, **heads})
    trusted = run.lab.intake.trusted_actors(run.repo)
    expander = expander_of(run, node, arguments.manifest)
    for name, head in heads.items():
        run.requests[name] = requested(run, name, head, base, trusted)
        expand(run, expander, name, run.requests[name])
    expected = planted_mutants(run, heads["twin"])
    run.proof["planted_mutants"] = expected
    run.write()
    say(f"{len(expected)} planted mutants named from {Path.cwd()}")
    if arguments.expand_only:
        print(json.dumps(run.proof["expansion"], indent=1))
        return 0
    pod, settings = pod_of(run, node, timeout)
    executors = run.lab.executors
    for owner, call in ((executors.venvs, "venv_for"), (executors, "_trampolined"), (executors, "_run")):
        timed(owner, call, run.proof["timings"])
    paced(run.lab.run_mutation, "trampoline")
    for name in GRADED:
        grade(run, pod, settings, name, heads[name])
    return finish(run, pod, heads, expected)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
