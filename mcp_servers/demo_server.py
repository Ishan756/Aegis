"""Local demonstration MCP server.

A stdio MCP server exposing three safe, read-only tools so the agent has
something real to discover and call before any external integration exists.

Safety properties, all deliberate:

- every tool is annotated ``read_only`` and none is annotated ``destructive``,
  so Aegis' policy treats them as low risk
- there is no shell, exec or command tool, and the policy layer would refuse one
  if a server ever offered it
- ``get_project_files`` is confined to a root directory, skips symlinks, caps
  depth and result count, so it cannot be used to walk the filesystem
- no secret or environment value is ever returned; ``get_system_info`` reports
  only fixed, non-sensitive fields

Run it directly for a manual smoke test::

    python mcp_servers/demo_server.py
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

#: Directory the project tools are allowed to read. Overridable so the server can
#: be pointed at a fixture during tests.
ROOT = Path(os.environ.get("AEGIS_DEMO_ROOT", ".")).expanduser().resolve()

MAX_FILES = 200
MAX_DEPTH = 6
PRUNED = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"})

server = MCPServer("aegis-demo", instructions="Safe, read-only demonstration tools for Aegis.")


@server.tool(
    name="get_system_info",
    title="Get system information",
    description=(
        "Return non-sensitive information about the machine running this server: "
        "platform, Python version and processor. No environment variables, "
        "credentials or file contents are included."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
def get_system_info() -> dict[str, Any]:
    """Report platform details. Read-only and always succeeds."""
    return {
        "platform": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "server": "aegis-demo",
        "root": str(ROOT),
    }


@server.tool(
    name="get_project_files",
    title="List project files",
    description=(
        "List files under the configured project root, relative to it. Symlinks are "
        "skipped, traversal is depth-limited, and the result is capped, so this "
        "cannot be used to read arbitrary parts of the filesystem."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
def get_project_files(
    pattern: Annotated[
        str, Field(description="Optional substring filter applied to file paths.")
    ] = "",
    limit: Annotated[int, Field(description="Maximum paths to return.", ge=1, le=MAX_FILES)] = 50,
) -> dict[str, Any]:
    """List files under :data:`ROOT`. Read-only."""
    matches: list[str] = []

    for dirpath, dirnames, filenames in os.walk(ROOT, followlinks=False):
        current = Path(dirpath)
        if len(current.relative_to(ROOT).parts) >= MAX_DEPTH:
            dirnames.clear()
        # Never descend into a symlinked directory.
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in PRUNED and not (current / name).is_symlink()
        )

        for filename in sorted(filenames):
            path = current / filename
            if path.is_symlink():
                continue
            relative = path.relative_to(ROOT).as_posix()
            if pattern and pattern not in relative:
                continue
            matches.append(relative)
            if len(matches) >= limit:
                return {
                    "files": matches,
                    "count": len(matches),
                    "truncated": True,
                    "root": str(ROOT),
                }

    return {"files": matches, "count": len(matches), "truncated": False, "root": str(ROOT)}


@server.tool(
    name="get_project_status",
    title="Summarise project status",
    description=(
        "Summarise the project root: file count by extension, presence of common "
        "manifests, and the most recently modified file. Derived purely from the "
        "filesystem; no git or shell command is executed."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
def get_project_status() -> dict[str, Any]:
    """Summarise the project at :data:`ROOT`. Read-only."""
    extensions: dict[str, int] = {}
    total = 0
    newest_path: str | None = None
    newest_mtime = -1.0

    manifests = [
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "go.mod",
        "Cargo.toml",
        "Gemfile",
        "Dockerfile",
        "docker-compose.yml",
        "README.md",
    ]
    found_manifests: list[str] = []

    for dirpath, dirnames, filenames in os.walk(ROOT, followlinks=False):
        current = Path(dirpath)
        if len(current.relative_to(ROOT).parts) >= MAX_DEPTH:
            dirnames.clear()
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in PRUNED and not (current / name).is_symlink()
        )

        for filename in filenames:
            path = current / filename
            if path.is_symlink():
                continue
            total += 1
            suffix = path.suffix.lower() or "(none)"
            extensions[suffix] = extensions.get(suffix, 0) + 1
            if filename in manifests:
                found_manifests.append(path.relative_to(ROOT).as_posix())
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest_mtime = mtime
                newest_path = path.relative_to(ROOT).as_posix()

    top = sorted(extensions.items(), key=lambda item: (-item[1], item[0]))[:10]
    return {
        "root": str(ROOT),
        "file_count": total,
        "top_extensions": [{"extension": ext, "count": count} for ext, count in top],
        "manifests": sorted(found_manifests),
        "most_recent_file": newest_path,
    }


def main() -> int:
    """Serve over stdio, which is how Aegis launches MCP servers."""
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())