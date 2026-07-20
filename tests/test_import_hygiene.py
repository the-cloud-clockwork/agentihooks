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
import io, sys, importlib, traceback
_real_out, _real_err = sys.__stdout__, sys.__stderr__
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()
try:
    importlib.import_module({module!r})
except BaseException:
    # Restore the real streams before reporting. Letting the exception escape
    # under the redirect writes the traceback into the StringIO and discards
    # it, leaving the parent with exit 1 and an empty stderr — a failure that
    # states only that something broke, never what.
    captured = sys.stderr.getvalue()
    sys.stdout, sys.stderr = _real_out, _real_err
    sys.stderr.write(captured)
    traceback.print_exc()
    raise SystemExit(1)
out = sys.stdout.getvalue()
err = sys.stderr.getvalue()
sys.stdout, sys.stderr = _real_out, _real_err
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
    env = {
        **os.environ,
        "CLAUDE_HOOK_LOG_ENABLED": "false",
        # The suite-wide home-isolation fixture repoints $HOME. Where the
        # interpreter installs into the user site (~/.local/lib/pythonX/
        # site-packages — the layout on the self-hosted runner), that alone
        # drops every dependency from the child's sys.path and the probe
        # reports an import failure that says more about the fixture than the
        # module. Hand the child the parent's resolved path so it imports
        # exactly what pytest imported.
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
    }
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


class TestProbeHarness:
    """The probe itself is load-bearing: when it misreports, every module in
    this file becomes undiagnosable. These guard the two ways it has failed.
    """

    def test_import_failure_reports_the_exception(self, tmp_path: Path) -> None:
        """A failing import must surface its traceback on stderr.

        Redirecting stderr to a StringIO and letting the exception escape sends
        the traceback into the buffer and drops it — the parent then sees exit 1
        with an empty stderr, which says only that something broke.
        """
        probe = _PROBE_TEMPLATE.format(module="hooks._definitely_not_a_real_module")
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
            timeout=30,
        )
        assert proc.returncode != 0
        assert "ModuleNotFoundError" in proc.stderr, f"probe swallowed the traceback — stderr was {proc.stderr!r}"

    def test_probe_resolves_imports_independently_of_home(self, tmp_path: Path) -> None:
        """Dependencies must stay importable when $HOME is repointed.

        The suite-wide isolation fixture rewrites $HOME. Where the interpreter
        installs into the user site — the self-hosted runner's layout — that
        alone strips every dependency from the child's path, and every module
        importing one fails for a reason that has nothing to do with the module.
        """
        probe = _PROBE_TEMPLATE.format(module="hooks.mcp")
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "HOME": str(tmp_path / "elsewhere"),
                "CLAUDE_HOOK_LOG_ENABLED": "false",
                "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
            },
            timeout=30,
        )
        assert proc.returncode == 0, f"import broke under a rewritten $HOME:\n{proc.stderr}"
