"""``python -m wireface.phone.mcp``: the phone tools as a stdio MCP server, for harnesses that take MCP servers by
command (OpenCode, Codex). Claude Code gets the same tools in-process (backends/claude.py).

It holds no Twilio credentials and decides nothing: each call goes to the face that started it (WIREFACE_URL, with
its access token WIREFACE_TOKEN), where ``PhoneLine.agent_tool`` asks the user, checks the number and the limits, and
does it. So every harness gets exactly the same rules.
"""
from __future__ import annotations

import asyncio
import os

import aiohttp
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .line import AGENT_TOOLS

# texting and calling wait for the user to say yes: give them time
TIMEOUT_S = 600


def build(url: str, token: str) -> Server:
    async def list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool(name=n, description=d, input_schema=s) for n, d, s in AGENT_TOOLS])

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as s:
                async with s.post(f"{url}/api/phone/agent", json={"tool": params.name, "args": params.arguments or {}},
                                  headers={"X-Face-Token": token}) as r:
                    data = await r.json(content_type=None)
            error = data.get("error") if r.status == 200 else (data.get("error") or f"the face answered {r.status}")
        except (aiohttp.ClientError, TimeoutError) as e:
            data, error = {}, f"can't reach the face: {e or type(e).__name__}"
        text = f"Couldn't: {error}" if error else data["text"]
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=bool(error))

    return Server("phone", on_list_tools=list_tools, on_call_tool=call_tool)


async def main() -> None:
    server = build(os.environ["WIREFACE_URL"], os.environ["WIREFACE_TOKEN"])
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
