import json
from collections.abc import Mapping
from pathlib import Path

from scripts.headless.request import KEPT, SEALED, Request, execute

CLAUDE = "claude"
HOOKS_OFF = '{"disableAllHooks":true}'
READ_TOOLS = "Read,Grep,Glob,WebSearch,WebFetch"
CLAMPS = {SEALED: ["--tools", ""], "read-only": ["--tools", READ_TOOLS, "--permission-mode", "dontAsk"]}


def command(request: Request) -> list[str]:
    return [
        CLAUDE,
        "-p",
        *CLAMPS[request.clamp],
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--settings",
        HOOKS_OFF,
        "--no-session-persistence",
        "--disable-slash-commands",
        "--system-prompt",
        request.profile_prompt,
        "--json-schema",
        json.dumps(request.schema),
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        request.model,
        "--effort",
        request.effort,
        request.user_input,
    ]


def route(environ: Mapping[str, str]) -> dict[str, str]:
    from hooks.context.account_sessions import API_ACCOUNT, sessions_by_account
    from scripts import operator_env
    from scripts.claude_quota_balancer import select_credential

    pool = {**environ, **operator_env.accounts(environ)}
    credential = select_credential(pool, sessions=sessions_by_account(), exclude=[API_ACCOUNT]).credential
    child = {name: environ[name] for name in KEPT if name in environ}
    return {**child, credential.env_name: credential.token, "CLAUDE_CODE_OAUTH_TOKEN": credential.token}


def reply(stdout: str) -> object:
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            structured = event.get("structured_output")
            return event.get("result", "") if structured is None else structured
    return ""


def invoke(request: Request, child: dict[str, str], work: Path) -> object:
    return reply(execute(command(request), child).stdout)
