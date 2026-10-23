import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from scripts.hive import auth
from scripts.hive.config import _installed

ENV_KEY = "HIVE_LINKED_PROFILES"


def sources(home: Path) -> str:
    from scripts.hive.daemon import origin

    found = []
    for profile in _installed(home / "state.json").get("linked_profiles", []):
        path = Path(profile["path"]).resolve()
        root = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if root.returncode:
            raise auth.HiveError(f"linked profile {profile['name']} has no git repository")
        repo = Path(root.stdout.strip())
        remote = origin(repo)
        branch = subprocess.run(
            ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True
        ).stdout.strip()
        if not remote or not branch:
            raise auth.HiveError(f"linked profile {profile['name']} needs an origin and a branch")
        found.append(
            {
                "name": profile["name"],
                "url": f"https://{remote}.git",
                "ref": branch,
                "path": path.relative_to(repo).as_posix(),
            }
        )
    return json.dumps(found)


def install(environ: Mapping[str, str]) -> None:
    for profile in json.loads(environ.get(ENV_KEY, "[]")):
        name = profile["name"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError("linked profile name must be a single directory name")
        revision = hashlib.sha256(json.dumps([profile["url"], profile["ref"]]).encode()).hexdigest()
        repo = Path(environ["HIVE_REPO_ROOT"]) / ".linked-profiles" / name / revision
        path = (repo / profile["path"]).resolve()
        if not path.is_relative_to(repo.resolve()):
            raise ValueError("linked profile path must stay inside its repository")
        if not (repo / ".git").is_dir():
            repo.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--branch", profile["ref"], "--", profile["url"], str(repo)], check=True)
        else:
            subprocess.run(
                ["git", "-C", str(repo), "pull", "--ff-only", "--", profile["url"], profile["ref"]], check=True
            )
        if not path.resolve().is_relative_to(repo.resolve()):
            raise ValueError("linked profile symlink must stay inside its repository")
        subprocess.run(
            ["agentihooks", "link-profile", "link", str(path), "--name", name, "--no-append", "--no-init"], check=True
        )


if __name__ == "__main__":
    install(os.environ)
