"""ATLAS research API: the H2 slice of the engine API (PRD §6, §11).

Agents never call this directly. Each ATLAS MCP server is a thin client that
holds one scoped token; this service checks the token's scopes on every call
and refuses any request that reaches into the locked holdout. Hermes tool
filtering is a convenience layer on top, not the security boundary.
"""
