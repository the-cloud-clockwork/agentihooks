import argparse
import os
import shutil
import sys


def claude_environment(command: list[str]) -> dict[str, str]:
    from hooks.context.account_sessions import sessions_by_account
    from scripts import operator_env
    from scripts.claude_quota_balancer import RoutingError, route_requires_fable, select_credential
    from scripts.install import _load_claude_runtime_env

    _load_claude_runtime_env()
    os.environ.update(operator_env.accounts(os.environ))
    try:
        decision = select_credential(
            os.environ,
            include_fable=route_requires_fable(command),
            claude_bin=shutil.which("claude") or "claude",
            sessions=sessions_by_account(),
        )
    except RoutingError as exc:
        print(f"skill-eval: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc
    selected = decision.credential
    child = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AH_CC_TOKEN_") and name not in {"ANTHROPIC_API_KEY", "CLAUDECODE"}
    }
    child[selected.env_name] = os.environ[selected.env_name]
    child["CLAUDE_CODE_OAUTH_TOKEN"] = child[selected.env_name]
    child["AGENTIHOOKS_ROUTE_ACCOUNT"] = selected.account
    print(f"[skill-eval] account={selected.account}", file=sys.stderr, flush=True)
    return child


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agentihooks skill eval", description="Run skill evaluations with quota routed Claude authentication"
    )
    parser.add_argument("--agent", choices=("claude", "codex"), default="claude")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("an evaluation command is required after --")
    child = claude_environment(command) if args.agent == "claude" else os.environ
    os.execvpe(command[0], command, child)


if __name__ == "__main__":
    main()
