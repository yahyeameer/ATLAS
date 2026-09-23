# Phase H3: operations integration (against a simulated engine)

Built on 2026-09-23 against the pinned Hermes `v2026.9.21` (`d337b736`). PRD
roadmap row: *`atlas-operations`, cron jobs, alerts, ATLAS dashboard plugin,
audit hooks. Exit gate: simulated incident flows end to end.*

The trading engine does not exist yet. The PRD says not to build it until T0
finds an edge (§26), and T4 is where it gets built. So H3 is built against the
engine's documented operations interface (PRD §6, §21, §23), and served by a
**simulated engine** that implements that interface. Everything on the agent
side is real Hermes: profiles, MCP, cron, Kanban, the plugin and the dashboard.
When T4 lands, the real engine serves the same five routes, and nothing on the
Hermes side should need to change.

## Status

| | |
| --- | --- |
| Exit gate: simulated incidents end to end | **Passed.** 29 of 29 checks: `python tests/hermes/h3_gate.py --hermes <hermes>` |
| Unit tests | 152 in `tests/ops/` and `tests/hermes/test_ops_install.py`. The full suite passes: 596 tests. |
| Earlier gates | The H1 and H2 gates still pass. |
| Real engine, MT5, Telegram | Not built or not reachable here. See "Deferred". |

## What was built

| Path | What it is |
| --- | --- |
| `atlas_engine/ops/health.py` | The PRD §23 state machine: NORMAL, DEGRADED, HALT or KILL from telemetry, with per-symbol states and the §23 thresholds. The real engine should reuse it in T4. |
| `atlas_engine/ops/sim.py` | The simulated engine. It keeps a JSON state file under a file lock, and supports 10 injectable faults (MT5 disconnect, spread spike, tick gap, reconciliation mismatch, clock drift, DB write failure, heartbeat silence, daily loss, drawdown, Jev latency). It logs state changes as events. KILL flattens and disables trading. Only the operator CLI can re-enable, and it refuses while HALT or KILL persists. Every response says `"source": "simulated"`. |
| `atlas_api/ops.py` | The engine operations API: 5 routes (`operations/status`, `health`, `reconciliation`, `events`, `disable_trading`) and 2 scopes (`ops:read`, `ops:disable_trading`). It has its own token file, separate from the research API. CLI: `atlas-engine-sim` (serve, token, init, show, inject, clear, enable, kill). |
| `atlas_mcp` | The new `atlas-operations` server: `system_status`, `health_state`, `reconciliation_report`, `disable_trading(reason)`, exactly as PRD §6 lists them. |
| `atlas_plugins/hooks.py` | Hermes hooks. They audit every ATLAS MCP call, count model tokens per board, and raise alerts on: a `disable_trading` call, a refused call, blocked or completed atlas-ops cards, and a crashed worker on any board. |
| `atlas_plugins/jobs.py` | The cron scripts: the 15-minute health check, the alert relay, the hourly reconciliation report, and scheduled cards. |
| `atlas_plugins/dashboard.py` and `atlas_plugins/hermes-plugin/` | The `atlas` Hermes plugin, installed into every profile. It includes the ATLAS dashboard tab (`/api/plugins/atlas/overview`). |
| `atlas-skills/incident-triage/` | Skill 9, pinned to operations-monitor. |
| `deploy/hermes/cron.yaml`, `ops.yaml` | The PRD §10 schedule, plus budgets and alert settings. |
| `deploy/hermes/bootstrap.py` | Now also issues engine tokens, installs the plugin and `ops.yaml`, and creates, updates or removes the cron jobs. |
| `tests/ops/`, `tests/hermes/test_ops_install.py`, `tests/hermes/h3_gate.py` | Unit tests and the Hermes drill. |

### Who can do what

| Profile | atlas-operations tools | Engine token scopes |
| --- | --- | --- |
| operations-monitor | all 4 | `ops:read`, `ops:disable_trading` |
| execution-engineer | `system_status`, `health_state`, `reconciliation_report` | `ops:read` |
| operations-monitor's cron scripts | (not an agent) | `ops:read` |
| atlas-orchestrator's dashboard tab | (not an agent) | `ops:read` |

Nothing can enable trading, flatten, clear a kill or change a limit. No route or
scope for those exists, and a test checks the route list. Engine tokens and
research tokens use disjoint scope sets and separate files, so a research token
gets 401 from the engine API. `atlas-emergency` (PRD §6) is not built. It is for
the operator's own profile only and needs the real engine's operator
authentication.

## How an incident flows

1. **Every 15 minutes**, `atlas-health-check` runs as a no-agent cron script on
   operations-monitor. It reads `operations/status` and `operations/health` with
   a read-only token, and costs no model tokens.
2. **When the state changes**, it writes one alert. HALT, KILL and "engine
   unreachable" also open an atlas-ops card for operations-monitor with the
   `incident-triage` skill. The card uses an idempotency key, so repeat checks
   during the same incident do not open more cards. If the incident is still
   open after 120 minutes, it sends a reminder. It also alerts when the engine
   returns to NORMAL.
3. **The dispatcher starts operations-monitor on the card.** That is the "LLM
   only on anomaly" of PRD §10. The skill reads health, status and
   reconciliation, then calls `disable_trading` once if new trades are still
   enabled. It then blocks the card with exactly what the operator must do.
4. **The plugin's hooks** write each MCP call to the audit log (profile, board,
   card, tool, arguments, outcome). They alert on the disable and on the
   blocked card.
5. **Every minute**, `atlas-alert-relay` sends new alerts to Telegram in one
   message. It skips the ones the health check already delivered itself, and it
   checks the token budgets first. A Telegram message never authorizes anything
   (decision 7).

Where it writes, on the agent host (`<hermes root>/atlas/`):

- `audit/agent_actions.jsonl`: every MCP call.
- `audit/usage.jsonl`: model tokens per board.
- `alerts/alerts.jsonl`: every alert.
- `reports/reconciliation/`: the hourly reports.
- `state/`: cursors and last-seen states.

Agents can't edit these files: their file tools are fenced to Kanban workspaces
and their terminals run in Docker.

## Exit gate

`tests/hermes/h3_gate.py` runs the installer into a throwaway Hermes home. It
starts the ops API over the simulated engine, runs the cron jobs with `hermes
cron run`, and lets the Kanban dispatcher run the cards with a scripted model.
All 29 checks pass:

- **Setup:** the plugin passes `hermes plugins doctor` with 5 hooks.
- **Baseline:** the health check reaches the engine with its own token and stays
  silent. The reconciliation report is written and stays silent while clean.
- **HALT (MT5 disconnect):**
  - The health check sends one alert and opens one incident card, with the
    skill and the paper tenant. A second check adds nothing.
  - The worker reads health, status and reconciliation, then disables trading.
    The engine records `operations-monitor/atlas-operations` as the one who
    disabled it.
  - The worker blocks the card in a single run.
  - The four calls are in the audit log with the board and card. The disable
    and the block both raised alerts from inside the worker.
- **Relay:** it delivers those alerts once, and not the health check's own. The
  token overrun on atlas-ops is one critical alert.
- **Recovery:** the operator clears the fault and re-enables, and a RECOVERED
  alert follows.
- **KILL (daily loss):** the engine flattens and disables itself. A second card
  opens. The worker sees trading already disabled, does not disable again, and
  blocks.
- **Scope:**
  - operations-monitor is offered exactly its 4 tools and sees its skill.
  - execution-engineer is offered only the 3 read tools.
  - execution-engineer's own token gets 403 on `disable_trading`.
- **Scheduled cards:** running the weekly calibration job twice creates one
  card for the week.
- **Dashboard:** a real `hermes dashboard` mounts the plugin's API. The engine
  panels read through the dashboard's own read token. The audit, alert, budget
  and experiment panels are filled, and the deferred panels say what they wait
  for.
- **Engine down:** the health check sends an UNREACHABLE alert and opens a
  card. The worker blocks for the operator, and its failed call is audited as
  an error.

## Decisions and findings

- **The engine API is its own API.** It is separate from the research API, with
  its own token file and scopes, and it runs on port 8742. `atlas-engine-sim
  serve` stands in for the engine until T4.
- **A silent heartbeat is treated as HALT.** PRD §23 says 60 s of silence pages
  the operator, but doesn't name a state for it. HALT stops new trades and
  pages, so I chose that. Change it in `health.py` if you want something else.
- **HALT does not disable trading by itself.** It only blocks new trades while
  it lasts. That is what §23 says, and it is why the incident skill adds the
  explicit disable. KILL disables, as §23 says.
- **The installer creates the cron jobs, not the agents.** The managed policy
  turns off the agents' `cronjob` toolset, which is intended. The jobs are
  `--no-agent` scripts. The ones that need a later phase (loss clusters, MFE/MAE,
  strategy vs expectation, the daily report, calibration, the robustness
  refresh) are gated and only installed with `--enable-gated`.
- **The health check's engine token reaches the script via
  `terminal.env_passthrough`**, only on operations-monitor. That profile has no
  terminal, code or file tools, so no agent sandbox sees the token.
- **Hermes' interpreter doesn't need ATLAS installed.** The installer records
  this checkout's path next to the plugin and in each cron script.
- **Findings from the drill:**
  - The first drill run showed that `kanban_block` has no `metadata` argument. A
    worker passing it fails its terminal call, which Hermes counts as a crash;
    after three crashes Hermes gives up on the card. The skill now puts the
    incident facts in the block reason.
  - The plugin manifest key is `provides_hooks`, not `hooks`. `plugins doctor`
    warned about this.
  - Crossing both budget thresholds at once now sends one alert.

## Install on the agent host

1. For drills, run the simulated engine as the engine user:
   `atlas-engine-sim --state /srv/atlas/engine/sim.json init`, then
   `atlas-engine-sim --state /srv/atlas/engine/sim.json serve --tokens /srv/atlas/engine/engine-tokens.yaml`.
2. Re-run the installer with the engine settings:
   `... bootstrap.py ... --engine-tokens /srv/atlas/engine/engine-tokens.yaml --engine-url http://127.0.0.1:8742`.
   Add `--cron-deliver local` while you are drilling, so nothing goes to
   Telegram.
3. Set budgets in `deploy/hermes/ops.yaml`, then re-run the installer. The
   budgets are placeholders now.
4. Inject a fault: `atlas-engine-sim --state ... inject mt5_disconnect seconds=120`.
   Then watch the ATLAS tab in `hermes dashboard` and the atlas-ops board.

## Deferred (and why)

| What | Waits for |
| --- | --- |
| The real engine behind the ops API: real MT5 link, reconciliation against the broker, heartbeats, watchdog EA | T4 |
| An alert path that does not go through the agent host (engine to operator by email or SMS). PRD §23 says the engine runs without the agent runtime; today every alert goes through Hermes. | T4 |
| `atlas-emergency` (flatten and similar, operator profile only, operator-authenticated) | T4 |
| `execution-debugging` and `mt5-reconciliation` skills | T4 |
| The `agent_actions` table in the engine database (PRD §24). The hooks write JSONL on the agent host for now. | T4 |
| Dashboard panels for equity and P/L, open risk and prop headroom, calibration, cost drift. Each shows what it waits for. | T2, T3, T4, T7 |
| The gated cron jobs (daily report, loss clusters, MFE/MAE, strategy vs expectation, calibration, robustness refresh) | T2, T4, T7 |

## Not verified here

- **Telegram delivery.** The drill delivers locally. The relay's output format
  is tested, but a real Telegram send is not.
- **The dashboard tab in a browser.** The API was tested through a real `hermes
  dashboard`, and `node --check` passes on the tab's script. But Hermes' web UI
  isn't built in this environment, so nobody has looked at the tab.
- **A live-model run of the incident drill.** The scripted model follows the
  skill's steps, which proves the wiring but not how a real model triages.
