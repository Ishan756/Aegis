"""Native agent tools.

Capabilities the agent can invoke directly from Python, as opposed to tools
exposed over MCP. Keep these deterministic and side-effect free where possible;
anything that mutates infrastructure belongs behind an MCP server so it can be
audited and permissioned. Populated in stage 6 and later.
"""
