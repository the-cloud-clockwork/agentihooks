import argparse
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from scripts.ci_runner_trust import ACTORS, validate_actors


def audit(approved: list[str], collaborators: list[dict], installations: list[dict]) -> dict:
    approved = validate_actors(approved)
    names = sorted({entry["login"] for entry in collaborators})
    apps = sorted({entry["app_slug"] + "[bot]" for entry in installations})
    return {
        "approved": approved,
        "collaborators": names,
        "apps": apps,
        "unapproved": sorted((set(names) | set(apps)) - set(approved)),
    }


def _api(endpoint: str) -> object:
    result = subprocess.run(["gh", "api", endpoint], check=True, capture_output=True)
    return json.loads(result.stdout)


def inventory(repo: str, api: Callable[[str], object] = _api) -> tuple[list[dict], list[dict]]:
    collaborators = []
    page = 1
    while True:
        entries = api(f"repos/{repo}/collaborators?page={page}&per_page=100")
        if not isinstance(entries, list):
            raise ValueError("collaborator inventory incomplete")
        collaborators.extend(entries)
        if len(entries) < 100:
            break
        page += 1
    installations = []
    page = 1
    while True:
        result = api(f"orgs/{repo.split('/')[0]}/installations?page={page}&per_page=100")
        entries = result["installations"]
        installations.extend(entries)
        if len(entries) < 100:
            if len(installations) != result["total_count"]:
                raise ValueError("app installation inventory incomplete")
            break
        page += 1
    return collaborators, installations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--actors", type=Path, default=Path(ACTORS))
    parser.add_argument("--report", type=Path, default=Path("access-report.json"))
    parser.add_argument("--alert", type=Path, default=Path("access-alert.txt"))
    args = parser.parse_args(argv)
    try:
        approved = json.loads(args.actors.read_text())
        collaborators, installations = inventory(args.repo)
        report = audit(approved, collaborators, installations)
        alert = "Unapproved GitHub access: " + ", ".join(report["unapproved"]) if report["unapproved"] else ""
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.CalledProcessError):
        report = {"error": "Access inventory could not be completed"}
        alert = f"GitHub access inventory could not be completed for {args.repo}; review the weekly audit."
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    args.alert.write_text(alert + "\n" if alert else "")
    print(json.dumps(report, indent=2))
    return int(bool(alert))


if __name__ == "__main__":
    raise SystemExit(main())
