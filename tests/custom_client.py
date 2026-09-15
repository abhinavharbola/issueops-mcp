import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    if len(sys.argv) < 2:
        print("usage: python tests/custom_client.py <tool_name> [json_arguments]")
        raise SystemExit(1)

    tool_name = sys.argv[1]
    arguments = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    server_params = StdioServerParameters(
        # Use the same interpreter running this script instead of a hardcoded
        # "python3", which is not guaranteed to exist on a stock Windows
        # Python install (only "python"/"py" are).
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        env=dict(os.environ),
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("available tools:", [t.name for t in tools.tools])
            result = await session.call_tool(tool_name, arguments)
            print(json.dumps([block.model_dump() for block in result.content], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
