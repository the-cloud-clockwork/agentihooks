import subprocess
from pathlib import Path


def recover(checkout: Path) -> None:
    log = checkout / ".git" / "gc.log"
    if not log.is_file():
        return
    subprocess.run(["git", "-C", str(checkout), "prune", "--expire=2.weeks.ago"], check=True, timeout=180)
    log.unlink()
    subprocess.run(
        ["git", "-C", str(checkout), "gc", "--cruft", "--prune=2.weeks.ago", "--quiet"], check=True, timeout=180
    )
