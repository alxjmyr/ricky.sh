"""Built-in tools shipped with the agent core."""

from ricky.tools.base import Tool
from ricky.tools.builtin.artifacts import ReadToolArtifactTool
from ricky.tools.builtin.files import (
    EditFileTool,
    GlobSearchTool,
    GrepSearchTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from ricky.tools.builtin.shell import RunShellTool
from ricky.tools.builtin.tasks import UpdateTasksTool


def builtin_tools() -> list[Tool]:
    """Construct the default built-in tool set."""
    return [
        RunShellTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListDirTool(),
        GlobSearchTool(),
        GrepSearchTool(),
        UpdateTasksTool(),
    ]


__all__ = [
    "EditFileTool",
    "GlobSearchTool",
    "GrepSearchTool",
    "ListDirTool",
    "ReadFileTool",
    "ReadToolArtifactTool",
    "RunShellTool",
    "UpdateTasksTool",
    "WriteFileTool",
    "builtin_tools",
]
