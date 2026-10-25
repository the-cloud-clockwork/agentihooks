import io
import json
import re
import subprocess
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
        f"unit (3.12, {shard})" in passed or f"unit-rerun (3.12, {shard})" in passed for shard in shards
    )


def fetch(repository: str, run: str, destination: Path, downloaded: dict) -> None:
    pages = api(f"repos/{repository}/actions/runs/{run}/artifacts?per_page=100")
    pending = [
        artifact
        for page in pages
        for artifact in page["artifacts"]
        if re.fullmatch(r"(?:durations|flakes)-3\.12-\d+", artifact["name"])
        and not artifact["expired"]
        and downloaded.get(artifact["name"]) != artifact["id"]
    ]

    def download(artifact):
        payload = api(f"repos/{repository}/actions/artifacts/{artifact['id']}/zip", binary=True)
        folder = destination / artifact["name"]
        folder.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in archive.namelist():
                if name in {"durations.json", "durations.sha256", "elapsed.json", "flakes.jsonl"}:
                    (folder / name).write_bytes(archive.read(name))
        return artifact["name"], artifact["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        downloaded.update(pool.map(download, pending))
