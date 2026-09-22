"""ATLAS MCP servers (PRD §6): thin, read-mostly clients of the research API.

Each server holds exactly one scoped token (``ATLAS_ENGINE_TOKEN``, from the
profile's MCP ``env``) and forwards calls to the API at ``ATLAS_API_URL``. No
server contains trading logic, and none has an order, risk, enable or holdout
tool.
"""
