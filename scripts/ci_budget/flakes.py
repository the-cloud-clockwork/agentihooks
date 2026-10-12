"""Tests the newest dev flake budget names as flaky in two dev runs within seven days, each added once as a ledger follow up.

The minute tick calls it; CI runners cannot reach the ledger, so the follow up is filed from the machine that can.
"""

from __future__ import annotations

import io
import json
import subprocess
import zipfile
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from scripts.ci_budget import defects
from scripts.swarm.store import SwarmError

if TYPE_CHECKING:
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.store import RedisStore, SwarmConfig

ARTIFACT = "flake-budget"
RAISED_TTL_S = 7 * 24 * 3600
READ_ERRORS = (*defects.READ_ERRORS, zipfile.BadZipFile)


def _newest(repo_dir: str, branch: str, run: Callable) -> str:
    jq = f'[.artifacts[] | select((.expired | not) and .workflow_run.head_branch == "{branch}")][0].id // empty'
    return run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/artifacts?name={ARTIFACT}&per_page=100", "--jq", jq],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def read_budget(repo_dir: str, branch: str, run: Callable) -> dict | None:
    artifact = _newest(repo_dir, branch, run)
    if not artifact:
        return None
    archive = run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/artifacts/{artifact}/zip"],
        cwd=repo_dir,
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    return json.loads(zipfile.ZipFile(io.BytesIO(archive)).read(f"{ARTIFACT}.json"))


def text(test: dict) -> str:
    path, *_, name = test["test"].split("::")
    function = name.removeprefix("test_").replace("_", " ").lower()
    folders = PurePosixPath(path).parent.parts[1:]
    module = " ".join([*folders, PurePosixPath(path).stem.removeprefix("test_")]).replace("_", " ").lower()
    return (
        f"The test {function} in the {module} tests passed only on a retry in {len(test['runs'])} dev runs "
        "within seven days."
    )


def refresh(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    ledger: LedgerClient,
    now_ms: int,
    run: Callable = subprocess.run,
    branch: str = "dev",
) -> list[str]:
    redis = store.redis
    if now_ms - int(redis.get(defects.key(slug, "flakes")) or 0) < defects.REFRESH_MS:
        return []
    redis.set(defects.key(slug, "flakes"), now_ms)
    try:
        found = read_budget(config.repo, branch, run)
    except READ_ERRORS as exc:
        defects._skipped("reading the flake budget", exc)
        return []
    actions = []
    for test in (found or {}).get("over_budget", []):
        raised = defects.key(slug, "flaky", test["test"])
        if redis.exists(raised):
            continue
        try:
            sent = ledger.followup(slug, text(test))
        except (*READ_ERRORS, SwarmError) as exc:
            defects._skipped("one flaky test", exc)
            continue
        redis.set(raised, now_ms, ex=RAISED_TTL_S)
        if sent is not False:
            actions.append("filed a ledger follow up for a test flaky in two dev runs within seven days")
    return actions
