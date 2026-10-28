import subprocess
from collections.abc import Mapping
from pathlib import Path

REFRESH_SECONDS = 600
PULL_TIMEOUT_SECONDS = 120


def refresh(last: float, now: float, environ: Mapping[str, str]) -> float:
    url = environ.get("HIVE_BUNDLE_URL")
    if not url or now - last < REFRESH_SECONDS:
        return last
    bundle = Path(environ["HIVE_REPO_ROOT"]) / "agentihooks-bundle"
    if not (bundle / ".git").is_dir():
        return last
    ref = environ.get("HIVE_BUNDLE_REF") or "dev"
    try:
        subprocess.run(
            ["git", "-C", str(bundle), "pull", "--ff-only", "--", url, ref],
            capture_output=True,
            timeout=PULL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    return now
