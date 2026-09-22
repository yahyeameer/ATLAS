# Phase H2: read-only MCP servers and the first skills

Built on 2026-09-22 against the pinned Hermes `v2026.9.21` (`d337b736`), on top
of the T0 backtester. PRD roadmap row: *`atlas-market`, `atlas-backtest`,
`atlas-journal`, `atlas-performance`; first 8 skills. Exit gate: holdout refusal
and scope tests pass.*

## Status

| | |
| --- | --- |
| Exit gate: scope and holdout tests | **Passed.** 220 tests in `tests/mcp/`. |
| Hermes integration run | **Passed.** 20 of 20 checks: `python tests/hermes/h2_gate.py --hermes <hermes>` |
| Profile and skill checks | 81 pass in `tests/hermes/test_mcp_skills.py`; the 88 H1 checks and the H1 gate still pass. |
| Real market data | Not available here (network policy). Every run in this phase used synthetic data, so no strategy result below means anything about markets. |

## What was built

| Path | What it is |
| --- | --- |
| `atlas_api/` | The research API. `auth.py` holds scoped tokens (only SHA-256 hashes on disk). `service.py` implements every route and does the scope and holdout checks. `http.py` serves JSON over localhost. CLI: `atlas-api`. |
| `atlas_mcp/` | The four MCP servers. They are thin clients: each holds one token and forwards to the API, and none contains research or trading logic. `scopes.py` maps each tool to the scope it needs. Run with `python -m atlas_mcp <server>`. |
| `atlas-skills/<name>/SKILL.md` | The first 8 skills, each with the PRD §7 sections. |
| `atlas-profiles/roster.yaml` | Now also lists each role's MCP servers, the exact tools per server, and its pinned skills. |
| `atlas-profiles/<role>/config.yaml` | `mcp_servers` entries plus the matching `platform_toolsets` names. MCP tools are kept direct, not deferred (see findings). |
| `deploy/hermes/bootstrap.py` | Also bundles each role's pinned skills into its distribution, and issues one API token per (profile, server). |
| `deploy/hermes/systemd/atlas-api.service` | Runs the API as its own user, `atlas-engine`. |
| `tests/mcp/` | Scope matrix, holdout refusal, budget, and MCP→HTTP end to end. |
| `tests/hermes/h2_gate.py` | Hermes workers call the MCP tools through Kanban against a live API. |

### Tools and who gets them

| Server | Tools | Scope |
| --- | --- | --- |
| `atlas-market` | `collect_market_state`, `get_bars` (M15/H1, ≤500 bars), `get_spread_stats` | `market:read` |
| `atlas-backtest` | `run_backtest`, `run_walk_forward` | `backtest:run` |
| | `monte_carlo`, `get_run_summary`, `list_runs` | `backtest:read` |
| `atlas-journal` | `query_trades`, `mfe_mae`, `loss_clusters` | `journal:read` |
| `atlas-performance` | `performance_summary` | `performance:read` |

| Profile | MCP servers (tools) | Token scopes | Skills |
| --- | --- | --- | --- |
| atlas-orchestrator | journal, performance | journal:read, performance:read | backtest-analysis |
| market-researcher | market | market:read | forex-market-analysis |
| strategy-researcher | backtest (all) | backtest:run, backtest:read | trend-pullback-, session-breakout-, liquidity-sweep-research, backtest-analysis |
| backtest-engineer | backtest (all) | backtest:run, backtest:read | backtest-analysis, mfe-mae-analysis |
| risk-analyst | backtest (read tools only), journal, performance | backtest:read, journal:read, performance:read | risk-review, backtest-analysis |
| performance-analyst | journal, performance | journal:read, performance:read | mfe-mae-analysis, backtest-analysis |
| data-engineer | market | market:read | data-quality |
| jev-analyst | backtest (all) | backtest:run, backtest:read | backtest-analysis |
| execution-engineer, operations-monitor | none yet (atlas-operations comes in H3, atlas-trading in T7) | none | none yet |

The 8 skills are forex-market-analysis, the three setup research skills, backtest-analysis, mfe-mae-analysis, risk-review and data-quality. I picked these because they cover what the H2 servers and the three T0 setups can support today. `prop-rule-analysis` waits for the prop-firm YAML (T3). `calibration-analysis` waits for T2, and the execution and MT5 skills wait for T4. PRD §7 says `risk-review` needs your review before it is pinned; this PR is that review.

## How the rules are enforced

- **The holdout is refused in the API, in two layers.** First, `window()` refuses any window named like the holdout, and any window ending after `holdout_start`. Second, the loader guard refuses any load that would reach the holdout. Run-based routes also refuse:
  - runs with a trade exiting after `holdout_start`;
  - summaries of kind `holdout`, or carrying a `holdout` key;
  - registry entries of kind `holdout`, which `list_runs` hides.

  The tests prove both layers separately. A spy loader shows that refused calls never touch data, and that a sweep of every route never loads a row past the holdout start.
- **Scope is checked in the API**, as the PRD requires, not in Hermes. Each method checks its scope before doing any work. The installer gives each (profile, server) pair its own token carrying only the scopes of the tools that profile was given. For example, risk-analyst's backtest token is `backtest:read` only, so even calling the API directly it cannot start a run (the gate checks this). Hermes' `tools.include` filter is the convenience layer on top.
- **No server has an order, risk, enable, flatten or holdout tool.** The tests check this across all four servers. No scope for trading, risk or the holdout exists at all.
- **Every backtest counts.** `run_backtest` and `run_walk_forward` check the monthly experiment budget before loading data, and return 429 once it is spent. Each run is appended to the registry with the token's name as `requested_by` and its Sharpe as a trial for the deflated Sharpe. Costs can only be stressed (`spread_mult` 1.0–3.0), and strategies run only on their configured symbols.
- **Secrets.** Hermes starts each MCP server with only `PATH`/`HOME`-style variables plus the two from its `env` block: the API URL and that server's own token. Tokens live in the profile's `.env` (0600), and the API's file holds only their hashes. Agent terminals run in Docker on the `atlas-agents` network, so they cannot reach the localhost API.

## Exit gate

`pytest tests/mcp` (220 tests):

- `test_scopes.py`: every route against every single-scope token, an all-scopes token and a no-scope token (403 unless the scope matches). Also: unknown or missing tokens get 401; refused calls touch neither the loader nor the registry; token files hold hashes only; re-issuing a token revokes the old one.
- `test_holdout.py`: 8 holdout window forms (by name, inside, one day over, one minute over, a timezone offset) on each window route, and on the resolver alone. Also: the boundary window is allowed; the loader guard works on its own; runs with holdout trades are refused on every run route (both trade-file names); holdout summaries and registry entries are refused or hidden; a full-route sweep never loads past the holdout.
- `test_service.py`: registry records, budget returns 429 without loading data, bad requests return 400 and are not recorded, walk-forward output, output caps, results in R after costs.
- `test_servers.py`: exact tool surface per server, the forbidden-tool list, each tool routing to its own server's API area, and MCP → HTTP → service end to end with per-profile tokens. Scope and holdout refusals reach the agent as tool errors.

Mutation check: each guard was removed in turn and the suite rerun. Removing the trade-file holdout check fails 12 tests, removing one route's scope check fails 7, removing the window end check fails 5 (the resolver tests; the loader guard still refuses the calls), removing the loader guard fails 1, and removing the budget check fails 1.

`tests/hermes/h2_gate.py` (Hermes integration, 20/20). It runs the installer into a throwaway Hermes home, starts the API on synthetic data, and lets the Kanban dispatcher run four cards with a scripted model:

- Each worker (strategy-researcher, risk-analyst, performance-analyst, market-researcher) was offered exactly its roster MCP tools, and saw its pinned skills and no others.
- strategy-researcher's backtest went Hermes → MCP → API, and was recorded under `strategy-researcher/atlas-backtest`.
- risk-analyst ran Monte Carlo on that run, and performance-analyst read the performance summary and MFE/MAE.
- All three holdout requests came back as `holdout_refused`, and the loader never read past the holdout start.
- risk-analyst's own token gets 403 on `backtest/run`, and only strategy-researcher's token started runs.

## Decisions and findings

- **Hermes defers MCP tools by default.** Out of the box, Hermes hides every MCP tool behind its `tool_search` bridge, and the first gate run failed on exactly that (`Unknown tool mcp__atlas_backtest__run_backtest`). ATLAS profiles have a handful of tools each, and the MCP tools are their core work, so every profile sets `tools.tool_search.enabled: off`. This also makes `todo` and `session_search` direct for the roles that have them.
- **The server command is `${ATLAS_MCP_PYTHON} -m atlas_mcp <server>`.** It is not an entry-point name, because the gateway's `PATH` under systemd need not include the venv's `bin`. The installer writes `ATLAS_MCP_PYTHON` (its own interpreter by default) into each profile's `.env`.
- **Skills are bundled at install time** into `skills/atlas/<skill>/` of each profile, and skills a role no longer pins are removed, because Hermes keeps skill directories a distribution stops shipping.
- **The API is the research API, not the trading engine API.** PRD §21 describes the engine API for intents and operations, and T0 has not found an edge, so that engine is still gated. This API only serves research data and recorded runs. The journal serves research trades for now; paper and live fills join it when the engine journal exists (T4, T7).
- **T0 dependency.** H2 is built on T0's branch (`claude/project-thread-h6ksfu`), including its pandas-3 timestamp fix, because the backtest tools run T0's code. Merging this PR also merges T0's commits.

## Install on the agent host

1. Install ATLAS with the MCP extra into the venv: `pip install -e '.[mcp]'`.
2. Re-run the installer with that venv's Python and the engine's token file:
   `/srv/atlas/venv/bin/python deploy/hermes/bootstrap.py --hermes /srv/atlas/venv/bin/hermes --hermes-home /srv/atlas/hermes --repo /srv/atlas/repo --api-tokens /srv/atlas/engine/api-tokens.yaml`
   Then `chown atlas-engine` the token file.
3. Install and start `deploy/hermes/systemd/atlas-api.service` before the gateways.
4. Check the wiring: `hermes -p strategy-researcher mcp test atlas-backtest`.

## Not verified here

- Tool results on real market data, and the timing of `run_walk_forward` over five years of M1 data. The MCP timeout is set to 30 minutes for `atlas-backtest`. Both wait on the data access that T0 is blocked on.
- A live-model run of the H2 drill. The scripted model proves the wiring, not how a real model uses the skills.
