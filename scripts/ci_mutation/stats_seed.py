import io
import re
import subprocess
import zipfile
from pathlib import Path

from scripts.ci_mutation.reuse import api as github_api


def api(endpoint: str):
    return github_api(endpoint, binary=endpoint.endswith("/zip"))


def seed(root: Path, folder: Path, base: str) -> int:
    pulls = api("pulls?state=closed&base=dev&sort=updated&direction=desc&per_page=30")
    folder.mkdir(parents=True, exist_ok=True)
    for pull in pulls:
        if not pull["merged_at"]:
            continue
        if subprocess.run(
            ["git", "merge-base", "--is-ancestor", pull["merge_commit_sha"], base],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode:
            continue
        head = pull["head"]["sha"]
        for workflow in ("test.yml", "mutation-preflight.yml"):
            runs = api(f"actions/workflows/{workflow}/runs?head_sha={head}&status=success&per_page=5")["workflow_runs"]
            for run in runs:
                if run["conclusion"] != "success" or run["head_sha"] != head:
                    continue
                artifacts = api(f"actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
                count = 0
                for artifact in artifacts:
                    if artifact["expired"] or not artifact["name"].startswith("mutation-test-stats-"):
                        continue
                    with zipfile.ZipFile(io.BytesIO(api(f"actions/artifacts/{artifact['id']}/zip"))) as zipped:
                        for name in zipped.namelist():
                            if re.fullmatch(r"[0-9a-f]{64}\.json", name):
                                (folder / name).write_bytes(zipped.read(name))
                                count += 1
                if count:
                    print(
                        f"Mutation stats seed: {count} records from merged pull request {pull['number']}, run {run['id']}"
                    )
                    return count
    print("Mutation stats seed: no compatible merged producer found")
    return 0
