import asyncio
import json
import subprocess

MAIL_TO = "AGENTIHOOKS_OPERATOR_EMAIL"
GATEWAY_KEY = "MCP_KEY_GATEWAY"
GATEWAY_URL = "LANGFUSE_MCP_URL"
DEFAULT_GATEWAY_URL = "https://10.10.30.200/mcp/"
TOOL = "notifications-send_email"
TIMEOUT_S = 10


def send(subject: str, text: str, environ: dict, repo: str) -> str:
    """An empty answer when sent, else why not."""
    to = environ.get(MAIL_TO) or _git_email(repo)
    if not to:
        return f"no operator email: set {MAIL_TO}"
    from hooks.integrations import mailer

    if not mailer.EmailClient.get_client().reachable():
        return _notify(to, subject, text, environ)
    result = mailer.send_email(to, subject, body=text)
    return "" if result.success else f"email failed: {result.error}"


def _notify(to: str, subject: str, text: str, environ: dict) -> str:
    bearer = environ.get(GATEWAY_KEY)
    if not bearer:
        return f"email failed: the SMTP relay is unreachable and {GATEWAY_KEY} is not set"
    url = environ.get(GATEWAY_URL) or DEFAULT_GATEWAY_URL
    try:
        reply = gateway_send(url, bearer, {"to": to, "subject": subject, "body": text})
    except Exception as exc:
        return f"email failed: the gateway notifications tool raised {exc}"
    return "" if reply.get("status") == "sent" else f"email failed: the gateway answered {json.dumps(reply)}"


async def _send(url: str, bearer: str, arguments: dict) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from scripts.swarm.langfuse_read import gateway_client

    headers = {"Authorization": f"Bearer {bearer}", "Host": "llm.homeofanton.com", "x-mcp-servers": "notifications"}
    async with asyncio.timeout(TIMEOUT_S):
        async with streamablehttp_client(
            url, headers=headers, timeout=TIMEOUT_S, httpx_client_factory=gateway_client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(TOOL, arguments)
    text = next((item.text for item in result.content if item.type == "text"), "")
    if result.isError:
        raise RuntimeError(text or "an error")
    return json.loads(text) if text else {}


def gateway_send(url: str, bearer: str, arguments: dict) -> dict:
    return asyncio.run(_send(url, bearer, arguments))


def _git_email(repo: str) -> str:
    try:
        found = subprocess.run(["git", "-C", repo, "config", "user.email"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return found.stdout.strip()
