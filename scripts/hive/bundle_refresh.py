import subprocess
from collections.abc import Mapping
from pathlib import Path

REFRESH_SECONDS = 600
PULL_TIMEOUT_SECONDS = 60


def refresh(last: float, now: float, environ: Mapping[str, str]) -> float:
    url = environ.get("HIVE_BUNDLE_URL")
    if not url or now - last < REFRESH_SECONDS:
        return last
    bundle = Path(environ.get("HIVE_REPO_ROOT") or Path.home() / "dev") / "agentihooks-bundle"
    if not (bundle / ".git").is_dir():
        return last
    before = _manifest(bundle)
    try:
        subprocess.run(
            ["git", "-C", str(bundle), "pull", "--ff-only", "--", url, environ.get("HIVE_BUNDLE_REF") or "dev"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PULL_TIMEOUT_SECONDS,
        )
        if _manifest(bundle) != before:
            subprocess.Popen(
                ["agentihooks", "deps", "ensure", "--quiet"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except (OSError, subprocess.SubprocessError):
        pass
    return now


def _manifest(bundle: Path) -> bytes:
    try:
        return (bundle / "deps.json").read_bytes()
    except OSError:
        return b""
