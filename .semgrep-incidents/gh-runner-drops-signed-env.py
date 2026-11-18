import subprocess
from collections.abc import Callable
from typing import Any


def dropping(run: Callable[..., Any]) -> Callable[..., Any]:
    def gh(argv: list[str], **options: Any) -> subprocess.CompletedProcess[Any]:
        # ruleid: gh-runner-drops-signed-env
        done = run(argv, capture_output=True, text=True, timeout=round(options["timeout"]))
        return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)

    return gh


def forwarding(run: Callable[..., Any]) -> Callable[..., Any]:
    def gh(argv: list[str], **options: Any) -> subprocess.CompletedProcess[Any]:
        signed = {"env": options["env"]} if "env" in options else {}
        # ok: gh-runner-drops-signed-env
        done = run(argv, capture_output=True, text=True, timeout=round(options["timeout"]), **signed)
        return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)

    return gh


def naming(run: Callable[..., Any]) -> Callable[..., Any]:
    def gh(argv: list[str], **options: Any) -> subprocess.CompletedProcess[Any]:
        # ok: gh-runner-drops-signed-env
        done = run(argv, capture_output=True, text=True, timeout=round(options["timeout"]), env=options.get("env"))
        return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)

    return gh
