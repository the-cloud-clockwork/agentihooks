import json
import re
import subprocess
from collections.abc import Callable

from scripts.swarm.store import SwarmError

MARKER = "<!-- staged-compatibility"
PULL = r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9]\d*)"
RUN = r"https://github\.com/([\w.-]+/[\w.-]+)/actions/runs/([1-9]\d*)"


def declaration(body: str) -> dict | None:
    _, marker, remainder = body.partition(MARKER)
    if not marker:
        return None
    encoded, closing, _ = remainder.partition("-->")
    if not closing or MARKER in remainder:
        raise SwarmError("staged compatibility: provide one complete declaration")
    try:
        proof = json.loads(encoded.lstrip())
    except ValueError as exc:
        raise SwarmError("staged compatibility: declaration must be valid JSON") from exc
    if not isinstance(proof, dict) or proof.get("complete") is not True:
        raise SwarmError("staged compatibility: finish all planned prerequisite edits and set complete to true")
    return proof


def target(proof: dict, role: str) -> tuple[str, str, str]:
    value = proof.get(role)
    if not isinstance(value, dict):
        raise SwarmError(f"staged compatibility: declare the {role} pull request and head")
    match = re.fullmatch(PULL, value.get("url", ""))
    head = value.get("head", "")
    if match is None or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise SwarmError(f"staged compatibility: declare a valid {role} URL and full head")
    return match[1], match[2], head


def jobs(path: str, read: Callable[[str], dict]) -> list:
    found = {}
    page = 1
    while True:
        result = read(f"{path}/jobs?per_page=100&page={page}")
        batch = result["jobs"]
        before = len(found)
        found.update((job["id"], job) for job in batch)
        if len(found) == result["total_count"]:
            return list(found.values())
        if len(found) <= before or len(batch) < 100:
            raise SwarmError("staged compatibility: incomplete compatibility job inventory")
        page += 1


def contracts(proof: dict, path: str, head: str, read: Callable[[str], dict]) -> None:
    names = proof.get("contracts")
    if not isinstance(names, list) or not names or any(not isinstance(name, str) or not name.strip() for name in names):
        raise SwarmError("staged compatibility: declare every affected contract job")
    results = jobs(path, read)
    missing = [
        name
        for name in names
        if not any(
            job["name"] == name
            and job["status"] == "completed"
            and job["conclusion"] == "success"
            and job["head_sha"] == head
            for job in results
        )
    ]
    if missing:
        raise SwarmError(f"staged compatibility: missing successful contract jobs: {', '.join(missing)}")


def verify(proof: dict, url: str, head: str, read: Callable[[str], dict]) -> None:
    repo, prerequisite, prerequisite_head = target(proof, "prerequisite")
    candidate_repo, candidate, candidate_head = target(proof, "candidate")
    match = re.fullmatch(RUN, proof.get("run", ""))
    if candidate_repo != repo or match is None or match[1] != repo or prerequisite == candidate:
        raise SwarmError(
            "staged compatibility: distinct prerequisite and candidate and their proof run must share a repository"
        )
    expected = {
        f"https://github.com/{repo}/pull/{prerequisite}": prerequisite_head,
        f"https://github.com/{repo}/pull/{candidate}": candidate_head,
    }
    if expected.get(url) != head:
        raise SwarmError("staged compatibility: declaration does not grade this exact pull request head")
    prior = read(f"repos/{repo}/pulls/{prerequisite}")
    current = read(f"repos/{repo}/pulls/{candidate}")
    if prior["head"]["sha"] != prerequisite_head:
        raise SwarmError("staged compatibility: prerequisite head changed; regenerate compatibility proof")
    if current["head"]["sha"] != candidate_head:
        raise SwarmError("staged compatibility: candidate head changed; regenerate compatibility proof")
    if url.endswith(f"/pull/{candidate}") and not prior["merged"]:
        raise SwarmError("staged compatibility: prerequisite must merge before candidate admission")
    comparison = read(f"repos/{repo}/compare/{prerequisite_head}...{candidate_head}")
    if comparison["status"] not in ("ahead", "identical"):
        raise SwarmError("staged compatibility: candidate must contain prerequisite head")
    path = f"repos/{repo}/actions/runs/{match[2]}"
    run = read(path)
    if run["head_sha"] != candidate_head:
        raise SwarmError("staged compatibility: stale proof head; regenerate compatibility proof")
    if run["status"] != "completed" or run["conclusion"] != "success":
        raise SwarmError("staged compatibility: require a completed successful compatibility run")
    contracts(proof, path, candidate_head, read)


def require(url: str, head: str, body: str, run=subprocess.run) -> None:
    from scripts.swarm.merge_queue import rest

    proof = declaration(body)
    if proof is None:
        return
    try:
        verify(proof, url, head, lambda path: rest(path, run))
    except (KeyError, TypeError) as exc:
        raise SwarmError(
            "staged compatibility: unreadable compatibility proof; repair the declaration or GitHub response"
        ) from exc
