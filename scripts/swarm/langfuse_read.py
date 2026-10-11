import asyncio
import json
import os


async def summary(slug: str, window: dict, timeout: float) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {
        "Authorization": f"Bearer {os.environ['MCP_KEY_GATEWAY']}",
        "Host": "llm.homeofanton.com",
        "x-mcp-servers": "langfuse_tools",
    }
    async with asyncio.timeout(timeout):
        async with streamablehttp_client(
            os.environ.get("LANGFUSE_MCP_URL", "http://10.10.30.200/mcp/"), headers=headers, timeout=timeout
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "langfuse_tools-swarm_error_latency_summary",
                    {
                        "swarm": slug,
                        "project": "agent-swarm",
                        "from_timestamp": window["fromTimestamp"],
                        "to_timestamp": window["toTimestamp"],
                    },
                )
                if result.isError:
                    raise RuntimeError("Langfuse summary tool failed")
                return json.loads(next(item.text for item in result.content if item.type == "text"))


def read_summary(slug: str, window: dict, timeout: float) -> dict:
    return asyncio.run(summary(slug, window, timeout))
