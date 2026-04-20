import logging
from typing import Optional, Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

logger = logging.getLogger(__name__)


class HttpMCPClient:
    """Handles MCP server connection and tool execution"""

    def __init__(self, mcp_server_url: str) -> None:
        self.server_url = mcp_server_url
        self.session: Optional[ClientSession] = None
        self._streams_context = None
        self._session_context = None
        logger.debug("HttpMCPClient instance created", extra={"server_url": mcp_server_url})

    @classmethod
    async def create(cls, mcp_server_url: str) -> 'HttpMCPClient':
        """Async factory method to create and connect MCPClient"""

        instance = cls(mcp_server_url)
        await instance.connect()
        return instance

    async def connect(self):
        """Connect to MCP server"""

        self._streams_context = streamable_http_client(self.server_url)
        read_stream, write_stream, _ = await self._streams_context.__aenter__()
        self._session_context = ClientSession(read_stream, write_stream)
        self.session = await self._session_context.__aenter__()
        init_result = await self.session.initialize()
        logger.info("Connected to MCP server", extra={"server_url": self.server_url, "init_result": init_result})

    async def get_tools(self) -> list[dict[str, Any]]:
        """Get available tools from MCP server"""

        if not self.session:
            logger.error("Attempted to get tools without an active session", extra={"server_url": self.server_url})
            raise RuntimeError(f"MCP client is not connected to MCP server at {self.server_url}")
        
        tools: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema
                }
            }
            for tool in (await self.session.list_tools()).tools
        ]

        logger.info("Retrieved tools from MCP server", extra={"server_url": self.server_url, "tools": tools})
        return tools

    async def call_tool(self, tool_name: str, tool_args: dict[str, Any]) -> Any:
        """Call a specific tool on the MCP server"""

        if not self.session:
            logger.error(
                "Attempted to call tool without an active session", 
                extra={
                    "server_url": self.server_url, 
                    "tool_name": tool_name
                }
            )
            raise RuntimeError(f"MCP client is not connected to MCP server at {self.server_url}")
        
        logger.info(
            "Calling tool on MCP server", 
            extra={
                "server_url": self.server_url, 
                "tool_name": tool_name, 
                "tool_args": tool_args
            }
        )
        call_result: CallToolResult = await self.session.call_tool(tool_name, tool_args)        
        content = call_result.content

        logger.debug(
            "Tool call result is TextContent", 
            extra={
                "server_url": self.server_url, 
                "tool_name": tool_name, 
                "content": content
            }
        )

        if len(content) == 1 and isinstance(content[0], TextContent):
            return content[0].text
        
        return content
