"""Arsenal tool registry - central store of all available MCP tools."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from henchmen.models.scheme import ArsenalRequirement


@dataclass
class ArsenalTool:
    name: str
    description: str
    category: str  # e.g. "code_intel", "code_edit", "git_ops"
    handler: Callable[..., Any]
    is_destructive: bool = False
    parameters: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """Central registry of all available Arsenal tools."""

    _tools: dict[str, ArsenalTool] = {}
    _categories: dict[str, list[str]] = {}  # category -> tool names

    @classmethod
    def register(cls, tool_def: ArsenalTool) -> None:
        """Register a tool definition."""
        cls._tools[tool_def.name] = tool_def
        if tool_def.category not in cls._categories:
            cls._categories[tool_def.category] = []
        if tool_def.name not in cls._categories[tool_def.category]:
            cls._categories[tool_def.category].append(tool_def.name)

    @classmethod
    def get_tools_for_requirement(cls, requirement: ArsenalRequirement) -> list[ArsenalTool]:
        """Filter tools by category membership and destructive flag."""
        result: list[ArsenalTool] = []
        for tool_set in requirement.tool_sets:
            tool_names = cls._categories.get(tool_set, [])
            for name in tool_names:
                tool_def = cls._tools.get(name)
                if tool_def is None:
                    continue
                if tool_def.is_destructive and not requirement.allow_destructive:
                    continue
                result.append(tool_def)
        return result

    @classmethod
    def get_tool(cls, name: str) -> ArsenalTool | None:
        """Look up a tool by name."""
        return cls._tools.get(name)

    @classmethod
    def list_categories(cls) -> list[str]:
        """Return a sorted list of all registered categories."""
        return sorted(cls._categories.keys())

    @classmethod
    def list_tools(cls, category: str | None = None) -> list[str]:
        """Return tool names, optionally filtered by category."""
        if category is not None:
            return list(cls._categories.get(category, []))
        return list(cls._tools.keys())

    @classmethod
    def clear(cls) -> None:
        """Remove all registered tools (useful for testing)."""
        cls._tools = {}
        cls._categories = {}


def tool(
    name: str,
    category: str,
    description: str,
    is_destructive: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to register a function as an Arsenal tool."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        import inspect

        sig = inspect.signature(func)
        parameters: dict[str, Any] = {}
        for param_name, param in sig.parameters.items():
            param_info: dict[str, Any] = {}
            if param.annotation is not inspect.Parameter.empty:
                param_info["annotation"] = param.annotation
            if param.default is not inspect.Parameter.empty:
                param_info["default"] = param.default
            parameters[param_name] = param_info

        tool_def = ArsenalTool(
            name=name,
            description=description,
            category=category,
            handler=func,
            is_destructive=is_destructive,
            parameters=parameters,
        )
        ToolRegistry.register(tool_def)
        return func

    return decorator
