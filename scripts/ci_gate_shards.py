import io
import json
import os
import re
import subprocess
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def api(endpoint: str, *, binary: bool = False) -> list | bytes:
    command = ["gh", "api", endpoint]
    if not binary:
        command.extend(["--paginate", "--slurp"])
    result = subprocess.run(command, check=True, capture_output=True, timeout=60)
    return result.stdout if binary else json.loads(result.stdout)


def ready(jobs: list[dict], shards: list[int]) -> bool:
    passed = {job["name"] for job in jobs if job.get("conclusion") == "success"}
    return bool(shards) and all(
        f"unit (3.12, {shard})" in passed or f"unit rerun (3.12, {shard})" in passed for shard in shards
    )


def watch(
    repository: str, run: str, attempt: str, shards: list[int], destination: Path, *, recover: bool = True
) -> None:
    downloaded = {}
    expected = {f"{kind}-3.12-{shard}" for kind in ("durations", "flakes") for shard in shards}
    units = {f"unit (3.12, {shard})" for shard in shards}
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        fetch(repository, run, destination, downloaded)
        pages = api(f"repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100")
        jobs = [job for page in pages for job in page["jobs"]]
        failures = [
            job["name"]
            for job in jobs
            if job["name"] in units and job.get("status") == "completed" and job.get("conclusion") != "success"
        ]
        if not recover and failures:
            raise RuntimeError(f"Original shards cannot recover on this event: {failures}")
        if ready(jobs, shards):
            fetch(repository, run, destination, downloaded)
            if missing := expected - downloaded.keys():
                raise RuntimeError(f"Missing shard reports: {sorted(missing)}")
            return
        time.sleep(5)
    raise RuntimeError("Shard verification timed out before every expected shard succeeded")


def fetch(repository: str, run: str, destination: Path, downloaded: dict) -> None:
    pages = api(f"repos/{repository}/actions/runs/{run}/artifacts?per_page=100")
    available = {
        artifact["name"]: artifact
        for page in pages
        for artifact in page["artifacts"]
        if re.fullmatch(r"(?:durations|flakes)-3\.12-\d+", artifact["name"]) and not artifact["expired"]
    }
    for name in downloaded.keys() - available.keys():
        del downloaded[name]
    pending = [artifact for name, artifact in available.items() if downloaded.get(name) != artifact["id"]]

    def download(artifact):
        payload = api(f"repos/{repository}/actions/artifacts/{artifact['id']}/zip", binary=True)
        folder = destination / artifact["name"]
        folder.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in ("durations.json", "durations.sha256", "elapsed.json", "flakes.jsonl"):
                target = folder / name
                target.unlink(missing_ok=True)
                if name in archive.namelist():
                    target.write_bytes(archive.read(name))
        return artifact["name"], artifact["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        downloaded.update(pool.map(download, pending))


def main() -> None:
    watch(
        os.environ["GITHUB_REPOSITORY"],
        os.environ["GITHUB_RUN_ID"],
        os.environ["GITHUB_RUN_ATTEMPT"],
        json.loads(os.environ["SHARDS"]),
        Path("shard-reports"),
        recover=os.environ.get("GITHUB_EVENT_NAME") == "merge_group",
    )


if __name__ == "__main__":
    main()
