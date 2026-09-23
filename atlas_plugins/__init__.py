"""ATLAS operations plugins for the agent host (PRD §9, §10, §24, H3).

- ``hooks``: the Hermes plugin hooks. Audit every ATLAS MCP call, count model
  tokens per board, and turn atlas-ops card events into alerts.
- ``jobs``: the cron scripts. Health check, alert relay, reconciliation report,
  and the scheduled Kanban cards.
- ``dashboard``: data and routes for the ATLAS tab in the Hermes dashboard.
- ``hermes/atlas/``: the plugin directory the installer copies into each profile.

None of this code runs in the trading hot path, and none of it can trade. The
only engine write it can reach is ``disable_trading``, and only through an
agent's own scoped token.
"""
