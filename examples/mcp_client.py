# Python code to test calling the tools through the mcp server
# Usage: MCP_URL=http://localhost:8030/mcp MCP_API_KEY=mcp_... MCP_SERVER=linux-01 python examples/mcp_client.py

import asyncio
import os

from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

MCP_URL = os.getenv("MCP_URL", "http://localhost:8010/mcp")
API_KEY = os.getenv("MCP_API_KEY", "")
SERVER = os.getenv("MCP_SERVER", "linux-01")


async def main():
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # List available tools
            tools = await session.list_tools()
            print([t.name for t in tools.tools])

            # List servers this client may use
            servers = await session.call_tool("list_servers", {})
            print(servers.content[0].text)

            # Call a read-only diagnostic command
            result = await session.call_tool("run_read_command", {"server": SERVER, "command": "df -h"})
            print(result.content[0].text)

            # Write command (rejected unless the server has allow_write: true)
            result = await session.call_tool("run_write_command", {"server": SERVER, "command": "touch /tmp/mcp_test"})
            print(result.content[0].text)

            # Get full system snapshot
            snapshot = await session.call_tool("get_system_snapshot", {"server": SERVER})
            print(snapshot.content[0].text)

asyncio.run(main())
