import subprocess
from pathlib import Path


def missing_expiry(checkout: Path) -> None:
    # ruleid: hive-prune-needs-expiry
    subprocess.run(["git", "-C", str(checkout), "prune"], check=True, timeout=180)


def immediate_expiry(checkout: Path) -> None:
    # ruleid: hive-prune-needs-expiry
    subprocess.run(["git", "-C", str(checkout), "prune", "--expire=now"], check=True, timeout=180)


def two_week_expiry(checkout: Path) -> None:
    # ok: hive-prune-needs-expiry
    subprocess.run(["git", "-C", str(checkout), "prune", "--expire=2.weeks.ago"], check=True, timeout=180)


def immediate_gc_expiry(checkout: Path) -> None:
    # ruleid: hive-prune-needs-expiry
    subprocess.run(["git", "-C", str(checkout), "gc", "--cruft", "--prune=now"], check=True, timeout=180)


def two_week_gc_expiry(checkout: Path) -> None:
    # ok: hive-prune-needs-expiry
    subprocess.run(
        ["git", "-C", str(checkout), "gc", "--cruft", "--prune=2.weeks.ago", "--quiet"], check=True, timeout=180
    )
