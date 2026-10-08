"""Write a package's acceptance case results and input manifest, naming the commit that holds every input."""

import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

UNCOMMITTED = "commit the case inputs first: results must name the commit that holds them"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _digests(root: Path, inputs: Sequence[str]) -> dict[str, str]:
    return {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in inputs}


def write(
    root: Path, output: Path, inputs: Sequence[str], evidence_class: str, cases: Sequence[tuple[str, Callable]]
) -> dict[str, bool] | None:
    """Each case's result as <name>-result.json plus manifest.json in output; None when an input is uncommitted."""
    if _git(root, "diff", "--quiet", "HEAD", "--", *inputs).returncode:
        return None
    stamp = {"tested_commit": _git(root, "rev-parse", "HEAD").stdout.strip(), "evidence_class": evidence_class}
    passed = {}
    for name, case in cases:
        result = {**stamp, **case()}
        passed[name] = result["passed"]
        (output / f"{name}-result.json").write_text(json.dumps(result, indent=2) + "\n")
    (output / "manifest.json").write_text(json.dumps({**stamp, "inputs": _digests(root, inputs)}, indent=2) + "\n")
    return passed
