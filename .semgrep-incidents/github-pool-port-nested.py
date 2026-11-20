import subprocess
from typing import Any

from scripts.swarm import github_reads
from scripts.tick import pool
from scripts.tick.adapters.github import GitHubSourceControl
from scripts.tick.deadline import Deadline


def nested_port(source: GitHubSourceControl, query: str) -> Any:
    with pool.limit("github"):
        # ruleid: github-pool-port-nested
        return source.graph(query, {}, deadline=Deadline.after(20))


def nested_port_with_caller(source: GitHubSourceControl, query: str) -> Any:
    with pool.limit("github"), github_reads.calling("merge-queue"):
        # ruleid: github-pool-port-nested
        return source.graph(query, {}, deadline=Deadline.after(20))


def nested_read(args: list[str]) -> subprocess.CompletedProcess[Any]:
    with github_reads.calling("merge-queue"), pool.limit("github"):
        # ruleid: github-pool-port-nested
        return github_reads.rest("merge-queue", args)


def direct_port(source: GitHubSourceControl, query: str) -> Any:
    with github_reads.calling("merge-queue"):
        # ok: github-pool-port-nested
        return source.graph(query, {}, deadline=Deadline.after(20))


def direct_read(args: list[str]) -> subprocess.CompletedProcess[Any]:
    # ok: github-pool-port-nested
    return github_reads.rest("merge-queue", args)


def different_pool(source: GitHubSourceControl, query: str) -> Any:
    with pool.limit("redis"):
        # ok: github-pool-port-nested
        return source.graph(query, {}, deadline=Deadline.after(20))


def raw_runner(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    with pool.limit("github"):
        # ok: github-pool-port-nested
        return subprocess.run(args, check=True)
