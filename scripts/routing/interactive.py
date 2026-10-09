import shutil
import subprocess
from collections.abc import Callable, Mapping

from scripts.claude_config import claude_home
from scripts.routing import envs, master_account


def account(harness: str, environ: Mapping[str, str], run: Callable = subprocess.run) -> master_account.MasterAccount:
    if harness == "claude":
        from scripts.claude_quota_balancer import RoutingError
    else:
        from scripts.codex_router import RoutingError

    declared = master_account.load(environ).get(harness)
    if declared is None:
        raise RoutingError("interactive master account missing")
    if harness == "claude":
        try:
            (claude_home(environ) / ".credentials.json").stat()
        except OSError as exc:
            raise RoutingError("interactive login missing") from exc
    else:
        try:
            status = run(
                [shutil.which("codex") or "codex", "login", "status"],
                env=envs.codex_interactive_child(environ, declared.slug),
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RoutingError("interactive login missing") from exc
        if status.returncode:
            raise RoutingError("interactive login missing")
    return declared


def launch_claude(binary: str, args: list[str], environ: Mapping[str, str], report: str) -> None:
    import os

    from scripts.install import _claude_command, _write_route_report

    declared = account("claude", environ)
    _write_route_report(report, status="routed", account=declared.slug, placement="forced")
    print(f"[agenti] account={declared.slug} route=interactive", flush=True)
    os.execvpe(binary, _claude_command(binary, args), envs.claude_interactive_child(environ, declared.slug))
