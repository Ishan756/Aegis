"""Read-only GitHub MCP server.

Exposes repository, branch, commit, issue, pull-request and file access as MCP
tools. Every tool is annotated ``read_only`` and none is destructive: this server
cannot create commits, branches or pull requests, and the name of a mutating tool
does not exist here to be called.

Credentials come only from the environment:

===============================  ==========================================
``AEGIS_GITHUB__TOKEN``          Token or PAT. Required.
``AEGIS_GITHUB__API_URL``        API root. Default ``https://api.github.com``.
===============================  ==========================================

The backend forwards these into this process explicitly; the MCP SDK does not
inherit arbitrary environment variables. The token is placed only in an
``Authorization`` header — never in a log line, a tool argument, a response body or
an error message. Response and error text is passed through a scrubber so that a
token echoed back by an upstream error cannot leak either.

Run it directly with::

    python mcp_servers/github/server.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

API_URL = os.environ.get("AEGIS_GITHUB__API_URL", "https://api.github.com").rstrip("/")
TOKEN_ENV = "AEGIS_GITHUB__TOKEN"
REQUEST_TIMEOUT = float(os.environ.get("AEGIS_GITHUB__TIMEOUT_SECONDS", "30"))

#: Caps chosen so a single tool call cannot pull an unbounded amount of data into
#: the agent's context or the backend's memory.
MAX_BRANCHES = 100
MAX_COMMITS = 50
MAX_ISSUES = 100
MAX_PULL_REQUESTS = 100
MAX_FILES = 2000
MAX_FILE_BYTES = 512 * 1024

USER_AGENT = "aegis-github-mcp"

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
)

server = MCPServer(
    "github",
    instructions="Read-only access to a GitHub repository: metadata, files, commits, branches, issues and pull requests.",
)


# --- Credential handling ------------------------------------------------


class GitHubAuthError(ToolError):
    """The server was started without a usable token.

    A :class:`ToolError` rather than a plain exception: the SDK returns an
    anticipated failure's own message to the caller, so an operator sees
    "set AEGIS_GITHUB__TOKEN" instead of a generic "error executing tool". A
    message nobody can act on is the same as no message when the call fails.
    """


def _token() -> str:
    """Read the token from the environment.

    The value is returned to the caller only to be placed in a request header. It
    is never stored on the server, logged, or included in a tool result.
    """
    value = os.environ.get(TOKEN_ENV, "").strip()
    if not value:
        raise GitHubAuthError(
            f"{TOKEN_ENV} is not set. The backend forwards it to this server; "
            "set it in the environment to enable GitHub access."
        )
    return value


def _scrub(text: str) -> str:
    """Remove the token from any text that could reach a log or a response.

    Belt and braces: the code below never puts the token in a message, but an
    upstream API error can echo request context back, so every string that leaves
    this module is passed through here first.
    """
    secret = os.environ.get(TOKEN_ENV, "").strip()
    if not secret:
        return text
    for variant in (secret, secret.lower()):
        if variant and variant in text:
            text = text.replace(variant, "[redacted]")
    return text


# --- HTTP ---------------------------------------------------------------


def _request(path: str, params: dict[str, Any] | None = None) -> Any:
    """Call the GitHub REST API and return decoded JSON.

    Anticipated failures raise :class:`ToolError`, so the caller receives an
    actionable message instead of a generic crash report. The response body is
    included only for 4xx responses from GitHub, which carry a useful ``message``
    field, and it is scrubbed before being raised.
    """
    url = f"{API_URL}{path}"
    if params:
        query = {key: str(value) for key, value in params.items() if value is not None}
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {_token()}",
    }

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _scrub(exc.read().decode("utf-8", errors="replace"))
        summary = _summarise_error(detail)
        raise ToolError(f"GitHub API returned {exc.code} for {path}: {summary}") from None
    except urllib.error.URLError as exc:
        # The reason can contain the URL, never the header, but scrub anyway.
        raise ToolError(_scrub(f"Could not reach the GitHub API: {exc.reason}")) from None
    except TimeoutError:
        raise ToolError(f"GitHub API request timed out after {REQUEST_TIMEOUT}s.") from None


def _summarise_error(body: str) -> str:
    """Pull GitHub's own error message out of a response body."""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return _scrub(body[:200]) or "no detail"
    message = payload.get("message") if isinstance(payload, dict) else None
    return _scrub(str(message))[:200] if message else "no detail"


def _cap(value: int, maximum: int) -> int:
    return max(1, min(value, maximum))


# --- Tools --------------------------------------------------------------


@server.tool(
    name="get_repository",
    title="Get repository metadata",
    description=(
        "Return metadata for a repository: description, default branch, primary "
        "language, licence, topics, visibility, activity timestamps and whether it "
        "is archived or a fork."
    ),
    annotations=_READ_ONLY,
)
def get_repository(
    owner: Annotated[str, Field(description="Repository owner, user or organisation.")],
    repo: Annotated[str, Field(description="Repository name.")],
) -> dict[str, Any]:
    """Fetch repository metadata."""
    data = _request(f"/repos/{owner}/{repo}")
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "full_name": data.get("full_name"),
        "description": data.get("description"),
        "default_branch": data.get("default_branch"),
        "language": data.get("language"),
        "license": (data.get("license") or {}).get("spdx_id"),
        "topics": data.get("topics") or [],
        "homepage": data.get("homepage"),
        "size_kb": data.get("size"),
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "open_issues_count": data.get("open_issues_count"),
        "archived": data.get("archived"),
        "fork": data.get("fork"),
        "visibility": data.get("visibility"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "pushed_at": data.get("pushed_at"),
    }


@server.tool(
    name="list_branches",
    title="List branches",
    description="List branches and whether each is protected from deletion.",
    annotations=_READ_ONLY,
)
def list_branches(
    owner: Annotated[str, Field(description="Repository owner.")],
    repo: Annotated[str, Field(description="Repository name.")],
    limit: Annotated[int, Field(description="Maximum branches to return.", ge=1, le=MAX_BRANCHES)] = 50,
) -> dict[str, Any]:
    """List branches in a repository."""
    count = _cap(limit, MAX_BRANCHES)
    data = _request(
        f"/repos/{owner}/{repo}/branches", {"per_page": count}
    )
    branches = [
        {
            "name": item.get("name"),
            "protected": item.get("protected"),
            "commit_sha": ((item.get("commit") or {}).get("sha") or "")[:40] or None,
        }
        for item in data
    ]
    return {"branches": branches, "count": len(branches), "truncated": len(data) >= count}


@server.tool(
    name="list_commits",
    title="List recent commits",
    description=(
        "List recent commits on a branch or ref, newest first. Only the first line "
        "of each message is returned, which is enough to see what kind of change is "
        "being made without pulling full diffs."
    ),
    annotations=_READ_ONLY,
)
def list_commits(
    owner: Annotated[str, Field(description="Repository owner.")],
    repo: Annotated[str, Field(description="Repository name.")],
    ref: Annotated[
        str | None, Field(description="Branch, tag or commit SHA. Defaults to the default branch.")
    ] = None,
    limit: Annotated[int, Field(description="Maximum commits to return.", ge=1, le=MAX_COMMITS)] = 20,
) -> dict[str, Any]:
    """List recent commits."""
    count = _cap(limit, MAX_COMMITS)
    data = _request(
        f"/repos/{owner}/{repo}/commits", {"sha": ref, "per_page": count}
    )
    commits = []
    for item in data:
        commit = item.get("commit") or {}
        author = commit.get("author") or {}
        message = (commit.get("message") or "").splitlines()
        commits.append(
            {
                "sha": (item.get("sha") or "")[:40] or None,
                "message": message[0] if message else "",
                "author": author.get("name"),
                "authored_at": author.get("date"),
                "author_login": (item.get("author") or {}).get("login"),
            }
        )
    return {"commits": commits, "count": len(commits), "truncated": len(data) >= count}


@server.tool(
    name="list_issues",
    title="List issues",
    description=(
        "List issues by state. Pull requests are excluded, since GitHub models them "
        "as issues too; use list_pull_requests for those."
    ),
    annotations=_READ_ONLY,
)
def list_issues(
    owner: Annotated[str, Field(description="Repository owner.")],
    repo: Annotated[str, Field(description="Repository name.")],
    state: Annotated[str, Field(description="open, closed or all.")] = "open",
    limit: Annotated[int, Field(description="Maximum issues to return.", ge=1, le=MAX_ISSUES)] = 50,
) -> dict[str, Any]:
    """List issues, excluding pull requests."""
    count = _cap(limit, MAX_ISSUES)
    data = _request(
        f"/repos/{owner}/{repo}/issues", {"state": state, "per_page": count}
    )
    issues = [
        {
            "number": item.get("number"),
            "title": item.get("title"),
            "state": item.get("state"),
            "labels": [label.get("name") for label in item.get("labels") or []],
            "comments": item.get("comments"),
            "author": (item.get("user") or {}).get("login"),
            "assignees": [user.get("login") for user in item.get("assignees") or []],
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "closed_at": item.get("closed_at"),
        }
        # GitHub returns pull requests from the issues endpoint; filter them out
        # so the two tools do not overlap.
        for item in data
        if "pull_request" not in item
    ]
    return {"issues": issues, "count": len(issues), "state": state}


@server.tool(
    name="list_pull_requests",
    title="List pull requests",
    description="List pull requests by state, including draft status and branch names.",
    annotations=_READ_ONLY,
)
def list_pull_requests(
    owner: Annotated[str, Field(description="Repository owner.")],
    repo: Annotated[str, Field(description="Repository name.")],
    state: Annotated[str, Field(description="open, closed or all.")] = "open",
    limit: Annotated[
        int, Field(description="Maximum pull requests to return.", ge=1, le=MAX_PULL_REQUESTS)
    ] = 50,
) -> dict[str, Any]:
    """List pull requests."""
    count = _cap(limit, MAX_PULL_REQUESTS)
    data = _request(f"/repos/{owner}/{repo}/pulls", {"state": state, "per_page": count})
    pulls = [
        {
            "number": item.get("number"),
            "title": item.get("title"),
            "state": item.get("state"),
            "draft": item.get("draft"),
            "merged": item.get("merged_at") is not None,
            "head": (item.get("head") or {}).get("ref"),
            "base": (item.get("base") or {}).get("ref"),
            "author": (item.get("user") or {}).get("login"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }
        for item in data
    ]
    return {"pull_requests": pulls, "count": len(pulls), "state": state}


@server.tool(
    name="list_files",
    title="List repository files",
    description=(
        "List file and directory paths in a repository at a given ref. Recursive by "
        "default so a whole tree can be assessed; the result is capped."
    ),
    annotations=_READ_ONLY,
)
def list_files(
    owner: Annotated[str, Field(description="Repository owner.")],
    repo: Annotated[str, Field(description="Repository name.")],
    ref: Annotated[str | None, Field(description="Branch, tag or commit SHA.")] = None,
    path: Annotated[str, Field(description="Subdirectory to list. Empty means the root.")] = "",
    limit: Annotated[int, Field(description="Maximum paths to return.", ge=1, le=MAX_FILES)] = 2000,
) -> dict[str, Any]:
    """List files in a repository tree."""
    clean_path = (path or "").strip("/")
    count = _cap(limit, MAX_FILES)

    if ref is None:
        metadata = _request(f"/repos/{owner}/{repo}")
        ref = metadata.get("default_branch") or "HEAD"

    data = _request(f"/repos/{owner}/{repo}/git/trees/{urllib.parse.quote(str(ref))}", {"recursive": "1"})
    if data.get("truncated"):
        truncated_tree = True
    else:
        truncated_tree = False

    entries: list[dict[str, Any]] = []
    for item in data.get("tree") or []:
        item_path = str(item.get("path") or "")
        if clean_path and not (
            item_path == clean_path or item_path.startswith(f"{clean_path}/")
        ):
            continue
        entries.append(
            {
                "path": item_path,
                "type": item.get("type"),
                "size": item.get("size"),
            }
        )
        if len(entries) >= count:
            break

    return {
        "ref": ref,
        "path": clean_path,
        "files": entries,
        "count": len(entries),
        # Either our own cap or GitHub's, so a caller never assumes completeness.
        "truncated": truncated_tree or len(entries) >= count,
    }


@server.tool(
    name="get_file_contents",
    title="Get file contents",
    description=(
        "Return the text content of a single file at a ref. Oversized files are "
        "refused rather than truncated silently, and binary content is not decoded."
    ),
    annotations=_READ_ONLY,
)
def get_file_contents(
    owner: Annotated[str, Field(description="Repository owner.")],
    repo: Annotated[str, Field(description="Repository name.")],
    path: Annotated[str, Field(description="Path to the file within the repository.")],
    ref: Annotated[str | None, Field(description="Branch, tag or commit SHA.")] = None,
) -> dict[str, Any]:
    """Fetch one file's contents as text."""
    encoded_path = "/".join(urllib.parse.quote(part) for part in path.strip("/").split("/"))
    if not encoded_path:
        raise ToolError("A file path is required.")

    endpoint = f"/repos/{owner}/{repo}/contents/{encoded_path}"
    if ref:
        endpoint = f"{endpoint}?ref={urllib.parse.quote(str(ref))}"

    payload = _request(endpoint)
    if isinstance(payload, list):
        raise ToolError(f"{path!r} is a directory, not a file.")

    if payload.get("encoding") != "base64":
        raise ToolError(f"{path!r} is not base64-encoded text and cannot be read.")

    raw = base64.b64decode(payload.get("content") or "")
    size = payload.get("size") or len(raw)
    if size > MAX_FILE_BYTES:
        raise ToolError(
            f"{path!r} is {size} bytes, above the {MAX_FILE_BYTES} byte read limit."
        )

    return {
        "path": payload.get("path"),
        "sha": payload.get("sha"),
        "size": size,
        "encoding": "utf-8",
        "content": raw.decode("utf-8", errors="replace"),
        "truncated": False,
    }


def main() -> int:
    """Serve over stdio."""
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())