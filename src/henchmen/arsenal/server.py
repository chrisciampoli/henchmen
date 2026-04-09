"""Arsenal MCP server - serves curated tool subsets to Operatives."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from henchmen.arsenal.registry import ToolRegistry
from henchmen.models.scheme import ArsenalRequirement


class ArsenalServer:
    """MCP server that exposes a filtered set of Arsenal tools based on an ArsenalRequirement."""

    def __init__(self, requirement: ArsenalRequirement) -> None:
        self.requirement = requirement
        self.mcp = FastMCP("henchmen-arsenal")
        self._register_tools()

    def _register_tools(self) -> None:
        """Register only the tools allowed by the requirement onto the FastMCP server."""
        tools = ToolRegistry.get_tools_for_requirement(self.requirement)
        for tool_def in tools:
            # Capture tool_def in closure
            _handler = tool_def.handler
            _name = tool_def.name
            _description = tool_def.description

            # FastMCP registers tools via @mcp.tool decorator.
            # We call it dynamically by wrapping the handler.
            self.mcp.tool(name=_name, description=_description)(_handler)

    def get_app(self) -> Any:
        """Return the ASGI/HTTP app for mounting in FastAPI (Streamable HTTP transport)."""
        return self.mcp.streamable_http_app()

    def run(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Start the Streamable HTTP server."""
        import uvicorn

        app = self.get_app()
        uvicorn.run(app, host=host, port=port)
