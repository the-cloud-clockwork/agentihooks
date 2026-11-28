from collections.abc import Callable
from typing import Any


def full_pages(api: Callable[[str], list[Any]], repository: str, run: str, attempt: str) -> list[Any]:
    # ruleid: shard-verifier-full-jobs-page
    return api(f"repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100")


def latest_full_pages(gh_api: Callable[..., bytes], repository: str, run: str, attempt: str) -> bytes:
    # ruleid: shard-verifier-full-jobs-page
    return gh_api([f"repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?filter=latest&per_page=100"])


def small_pages(read_jobs: Callable[[str, int], list[Any]], repository: str, run: str, attempt: str) -> list[Any]:
    # ok: shard-verifier-full-jobs-page
    return read_jobs(f"repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs", 10)


def one_shrinking_page(gh_api: Callable[..., bytes], endpoint: str, size: int, page: int) -> bytes:
    # ok: shard-verifier-full-jobs-page
    return gh_api([f"{endpoint}?per_page={size}&page={page}"])


def full_artifact_pages(api: Callable[[str], list[Any]], repository: str, run: str) -> list[Any]:
    # ok: shard-verifier-full-jobs-page
    return api(f"repos/{repository}/actions/runs/{run}/artifacts?per_page=100")
