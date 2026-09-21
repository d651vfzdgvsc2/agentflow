"""MCP Server：把 agentflow 的工具注册表暴露成标准 MCP 工具。

这样外部 Agent（OpenCode / Claude / 其它 MCP 客户端）也能复用同一套能力，
与内部多 Agent 共用同一份注册表，保证"外部能调到的"= "Agent 能调到的"。

启动（stdio）：python -m mcpserver.server
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from mcp.server import Server, ServerRequestContext  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.types import (  # noqa: E402
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from tools.registry import default_registry  # noqa: E402

REGISTRY = default_registry()


def _build_tools() -> list[Tool]:
    return [
        Tool(name=spec.name, description=spec.description, inputSchema=spec.parameters)
        for spec in REGISTRY.specs()
    ]


TOOLS = _build_tools()


async def _handle_list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def _handle_call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
    try:
        result = REGISTRY.call(params.name, params.arguments or {})
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))])
    except Exception as e:  # noqa: BLE001
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))],
            isError=True,
        )


server = Server("agentflow", on_list_tools=_handle_list_tools, on_call_tool=_handle_call_tool)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
