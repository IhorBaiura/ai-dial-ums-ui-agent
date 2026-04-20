import logging
from typing import Optional, Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, TextContent

logger = logging.getLogger(__name__)


class StdioMCPClient:
    """Handles MCP server connection and tool execution via stdio"""

    def __init__(self, docker_image: str) -> None:
        self.docker_image = docker_image
        self.session: Optional[ClientSession] = None
        self._stdio_context = None
        self._session_context = None
        logger.debug("StdioMCPClient instance created", extra={"docker_image": docker_image})

    @classmethod
    async def create(cls, docker_image: str) -> 'StdioMCPClient':
        """Async factory method to create and connect MCPClient"""

        isinstance = cls(docker_image)
        await isinstance.connect()
        return isinstance

    async def connect(self):
        """Connect to MCP server via Docker"""

        server_params = StdioServerParameters(
            command="docker",
            args=["run", "--rm", "-i", self.docker_image]
        )

        self._stdio_context = stdio_client(server_params)
        read_stream, write_stream = await self._stdio_context.__aenter__()
        self._session_context = ClientSession(read_stream, write_stream)
        self.session = await self._session_context.__aenter__()
        init_result = await self.session.initialize()

        logger.info(
            "Connected to Stdio MCP server", 
            extra={
                "docker_image": self.docker_image, 
                "init_result": init_result
            }
        )

    async def get_tools(self) -> list[dict[str, Any]]:
        """Get available tools from MCP server"""

        if not self.session:
            logger.error(
                "Attempted to get tools without an active session", 
                extra={
                    "docker_image": self.docker_image
                }
            )
            raise RuntimeError(f"MCP client is not connected to MCP server with docker image {self.docker_image}")
        
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

        logger.info(
            "Retrieved tools from MCP server",
            extra={
                "docker_image": self.docker_image,
                "tools": tools
            }
        )
        return tools

    async def call_tool(self, tool_name: str, tool_args: dict[str, Any]) -> Any:
        """Call a specific tool on the MCP server"""

        if not self.session:
            logger.error(
                "Attempted to get tools without an active session", 
                extra={
                    "docker_image": self.docker_image
                }
            )
            raise RuntimeError(f"MCP client is not connected to MCP server with docker image {self.docker_image}")
        
        logger.info(
            "Calling tool on MCP server", 
            extra={
                "docker_image": self.docker_image, 
                "tool_name": tool_name, 
                "tool_args": tool_args
            }
        )

        call_result: CallToolResult = await self.session.call_tool(tool_name, tool_args)
        content = call_result.content

        if len(content) == 1 and isinstance(content[0], TextContent):
            return content[0].text
        
        return content
