"""Stdout discipline test (P0.3).

Importing any hooks/* module must NOT write to stdout. Claude Code parses
hook-process stdout as JSON; any module-level print/import-time warning
silently corrupts the parse and drops `additionalContext`.

Per upstream protocol (https://code.claude.com/docs/en/hooks.md):
- exit 0 + valid JSON → fields applied
- exit 0 + invalid JSON → harness logs error, fields NOT applied
- exit 1/non-2 → non-blocking, stderr in transcript

A noisy import means our broadcast injection silently disappears. This
test catches it at CI time before it ships.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"

# Modules that legitimately print on import are quarantined here. Empty by
# design — every entry is a future bug. Add ONLY with a justifying comment.
_ALLOWED_NOISY: frozenset[str] = frozenset()

# Pre-imports that must run before the target so package-init side effects
# (logger setup, env loading) execute under the captured stdout.
_PROBE_TEMPLATE = """
import io, sys, importlib
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()
mod = importlib.import_module({module!r})
out = sys.stdout.getvalue()
err = sys.stderr.getvalue()
sys.__stdout__.write(out)
sys.__stderr__.write(err)
"""


def _all_hook_modules() -> list[str]:
    """Return dotted module names for every .py under hooks/, excluding tests
    and __pycache__. Skips files with leading underscore at the package root
    so __main__ does not execute the CLI.
    """
    modules: list[str] = []
    for path in HOOKS_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "__main__.py":
            continue
        rel = path.relative_to(REPO_ROOT)
        dotted = ".".join(rel.with_suffix("").parts)
        modules.append(dotted)
    return sorted(modules)


@pytest.mark.parametrize("module", _all_hook_modules())
def test_module_import_does_not_write_to_stdout(module: str) -> None:
    if module in _ALLOWED_NOISY:
        pytest.skip(f"{module} is in _ALLOWED_NOISY quarantine")

    probe = _PROBE_TEMPLATE.format(module=module)
    env = {**os.environ, "CLAUDE_HOOK_LOG_ENABLED": "false"}
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.fail(f"{module}: import failed (exit {proc.returncode})\nstderr: {proc.stderr}\nstdout: {proc.stdout}")
    if proc.stdout:
        pytest.fail(f"{module}: writes to stdout on import — would corrupt hook JSON.\ncaptured stdout:\n{proc.stdout}")
