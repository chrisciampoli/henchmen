"""Arsenal - tool registry exposing development capabilities to Operatives.

Arsenal runs *inside* the Operative process (see CLAUDE.md): the agent loop
pulls handlers straight from :class:`ToolRegistry`. There is no Arsenal
service and no MCP transport.
"""

from henchmen.arsenal.registry import ArsenalTool, ToolRegistry, tool

__all__ = [
    "ArsenalTool",
    "ToolRegistry",
    "tool",
]
