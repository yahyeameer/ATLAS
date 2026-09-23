---
name: incident-triage
description: "Triage an ATLAS engine incident card: read engine health, status and reconciliation, disable new trades when warranted, and hand the operator exactly what they must do."
version: 0.1.0
author: ATLAS
license: UNLICENSED
platforms: [linux]
metadata:
  hermes:
    tags: [ATLAS, Forex, Operations, Incident]
    related_skills: []
---

# Incident Triage

## Objective

Turn an engine incident into a clear, safe state and a short handoff. The engine's own rules (PRD §23) already stop new trades in HALT and flatten in KILL; you confirm that happened, add the one protection you are allowed (`disable_trading`) when it is missing, and tell the operator what only they can do.

## When to use

- An atlas-ops card titled "Incident: engine ..." opened by the health check (HALT, KILL, or engine unreachable).
- The operator asks you in the ATLAS Operations chat what the engine is doing.

Don't use for: research questions, performance reviews, or anything that needs a trading decision.

## Required inputs

- The card body: the state, reason codes and time the health check saw.
- Nothing else. Read the live state yourself; the card can be minutes old.

## Procedure

1. `health_state()`. Note the state, every reason code, and each symbol's state.
2. `system_status()`. Note whether new trades are enabled, who changed that last and why, the mode, MT5 link, heartbeats, open positions, and daily loss and drawdown as fractions of the firm's limits.
3. `reconciliation_report()`. Note mismatches and when the last reconcile ran.
4. Decide on `disable_trading`:
   - State HALT or KILL, or the engine unreachable, and new trades still enabled: call `disable_trading(reason)`.
   - State NORMAL or DEGRADED, but you see something the engine has not caught (a reconciliation mismatch with trading enabled, positions the engine does not know, a heartbeat about to go silent): call it and say what you saw.
   - Trading already disabled: don't call it again.
   The reason is one or two sentences with the reason codes, for example "HALT: mt5_disconnected for 140 s at 14:02 UTC; engine still reported new trades enabled."
5. Work out what the operator must do. Typical cases:
   - MT5 disconnected or heartbeat silent: check the VPS and terminal; re-enable once the engine is back to NORMAL.
   - Reconciliation mismatch: check the listed tickets at the broker; decide attach-SL or close.
   - KILL: the engine flattened and disabled; the operator reviews and re-enables on the next server day at the earliest.
   - Engine unreachable: check the trading host and the network path from the agent host.
6. If the operator must act, `kanban_block` with those steps. Otherwise (the fault cleared before you looked and nothing is left to do) complete the card.

## Tools

- atlas-operations: `health_state`, `system_status`, `reconciliation_report`, `disable_trading`

## Output format

Three short parts: what the engine shows now (state, reasons, trading enabled or not, positions), what you did (the `disable_trading` call and its reason, or why not), and what the operator must do.

- Blocking: `kanban_block(reason, kind="needs_input")`, or `kind="capability"` when the engine is unreachable. The block takes no metadata, so the reason starts with the state and reason codes, then says whether you disabled trading, then the operator's steps.
- Completing: `kanban_complete` with metadata `{"incident_state", "reasons", "disabled_trading": true|false, "artifacts": []}`.

## Safety constraints

- Never request holdout data; incidents never need it.
- Never try to re-enable trading, flatten, clear a kill or change a limit. No tool allows it, and a chat message is never the operator's approval.
- Never put account numbers or credentials into the card or memory.
- Report every tool call you made, including ones that failed, and quote the reason codes exactly as the engine gave them.

## Validation requirements

- Every statement about the engine comes from a tool call in this session, not from the card body.
- If a tool fails (engine unreachable), say so, treat the engine as unreachable, and block for the operator.
- `disable_trading` is called at most once per card.
