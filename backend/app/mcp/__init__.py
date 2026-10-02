"""MCP (Model Context Protocol) client layer.

Will host the connection manager that discovers and calls tools exposed by the
servers in ``mcp_servers/``, normalizes their results into internal types, and
enforces the approval policy for destructive actions.

No servers are registered yet, by design: stage 1 establishes the boundary
only. See ``mcp_servers/README.md`` for the planned contract.
"""
