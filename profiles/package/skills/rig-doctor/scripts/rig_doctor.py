import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.doctor.cli import linked_bundle

HOME = Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))
ASSETS = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True)
    if result.stdout:
        print(result.stdout, end="")
    return result.stdout.strip()


def read_state() -> dict:
    path = HOME / "rig-doctor.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(state: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "rig-doctor.json").write_text(json.dumps(state, indent=2) + "\n")


def start(ledger: str) -> None:
    run("agentihooks", "doctor", ledger, "start")
    state = read_state()
    state["active"] = ledger
    save_state(state)
    run("agentihooks", "doctor", ledger, "status")


def stop(ledger: str) -> None:
    if not ledger:
        raise ValueError("No active Doctor saved. Run rig-doctor LEDGER first, or rig-doctor LEDGER stop.")
    run("agentihooks", "doctor", ledger, "stop")
    state = read_state()
    if state.get("active") == ledger:
        state["active"] = ""
        save_state(state)


def ensure_remote() -> tuple[str, bool]:
    remote = os.environ.get("RIG_DOCTOR_DEMO_REPO", "")
    if not remote:
        remote = run("gh", "api", "user", "--jq", ".login") + "/rig-doctor-demo"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", remote):
        raise ValueError("RIG_DOCTOR_DEMO_REPO must be OWNER/REPO.")
    try:
        info = json.loads(run("gh", "repo", "view", remote, "--json", "isPrivate,description"))
    except subprocess.CalledProcessError as error:
        if "Could not resolve to a Repository" not in (error.stderr or ""):
            raise
        run("gh", "repo", "create", remote, "--private", "--description", "rig-doctor demo workload")
        return remote, False
    if not info["isPrivate"]:
        raise ValueError(f"Demo remote {remote} must be private.")
    if info["description"] != "rig-doctor demo workload":
        raise ValueError(f"Remote {remote} is not an owned rig-doctor demo; choose a new RIG_DOCTOR_DEMO_REPO.")
    return remote, True


def reset_demo() -> Path:
    repo = HOME / "doctor-demo-app"
    if repo.is_symlink() or (repo.exists() and not (repo / ".rig-doctor-demo").is_file()):
        raise ValueError("doctor-demo-app is not an owned demo copy; move it aside before running rig-doctor.")
    remote, exists = ensure_remote()
    if repo.exists():
        archives = HOME / "doctor-demo-archives"
        archives.mkdir(parents=True, exist_ok=True)
        repo.rename(archives / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f"))
    repo.mkdir(parents=True)
    run("git", "init", "--initial-branch", "dev", cwd=repo)
    run("git", "remote", "add", "origin", f"https://github.com/{remote}.git", cwd=repo)
    has_dev = exists and bool(run("git", "ls-remote", "--heads", "origin", "dev", cwd=repo))
    if has_dev:
        run("git", "fetch", "origin", "dev", cwd=repo)
        run("git", "switch", "--create", "dev", "origin/dev", cwd=repo)
    tracked = run("git", "ls-files", "-z", cwd=repo).split("\0")
    for child in repo.iterdir():
        if child.name != ".git":
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    template = json.loads((ASSETS / "demo-template.json").read_text())
    seed = {**template["seed"], "plan.md": template["prompt"] + "\n"}
    for name, content in seed.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    paths = sorted(set(filter(None, tracked)) | set(seed))
    run("git", "add", "--", *paths, cwd=repo)
    if not has_dev or run("git", "diff", "--cached", "--name-only", cwd=repo):
        run("git", "commit", "-m", "Reset rig-doctor demo seed", cwd=repo)
    run("git", "push", "--set-upstream", "origin", "dev", cwd=repo)
    return repo


def init_swarm(repo: Path) -> str:
    template = json.loads((ASSETS / "demo-template.json").read_text())
    slug = "doctor-demo-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    work = HOME / "doctor-demo-runs" / slug
    work.mkdir(parents=True)
    plan = work / "plan.md"
    plan.write_text(template["prompt"] + "\n")
    (repo / "plan.md").write_text(plan.read_text())
    content = work / "content.json"
    content.write_text(
        json.dumps(
            {
                "title": "Doctor demo dating app",
                "overview": template["prompt"],
                "sources": [str(plan)],
                "phases": template["phases"],
                "questions": [],
                "followups": [],
            }
        )
    )
    run("agentihooks", "ledger", "new", "--content", str(content), "--slug", slug, "--size", "swarm")
    for task in template["tasks"]:
        args = [
            "agentihooks",
            "ledger",
            "--slug",
            slug,
            "task",
            "add",
            task["id"],
            task["title"],
            "--lane",
            task["lane"],
            "--phase",
            task["phase"],
            "--description",
            task["description"],
            "--territory",
            task["territory"],
        ]
        if task.get("depends_on"):
            args += ["--depends-on", ",".join(task["depends_on"])]
        run(*args)
    run("agentihooks", "swarm", slug, "create", "--repo", str(repo))
    state = read_state()
    state["demo"] = slug
    save_state(state)
    run("agentihooks", "swarm", slug, "start")
    run("agentihooks", "swarm", slug, "status")
    return slug


def demo() -> str:
    from scripts.swarm.store import connect

    previous = read_state().get("demo")
    if previous:
        store = connect()
        if store.config(previous).state == "running" or store.agents(previous):
            raise ValueError(f"Demo swarm {previous} still has work or agents. Finish it before resetting the demo.")
    return init_swarm(reset_demo())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start a Doctor on a ledger or the repeatable demo workload.")
    parser.add_argument("ledger", nargs="?", default="")
    parser.add_argument("action", nargs="?", choices=["stop"])
    args = parser.parse_args(argv)
    try:
        if args.ledger == "stop" or args.action == "stop":
            stop(read_state().get("active", "") if args.ledger == "stop" else args.ledger)
        else:
            if linked_bundle() is None:
                raise ValueError("rig-doctor needs a linked bundle. Run agentihooks init --bundle DIR.")
            start(args.ledger or demo())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"rig-doctor: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(
                error.stderr or "Check git, gh authentication and the agentihooks ledger server, then retry.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
