# Phase T4: trading engine, MT5 adapter, reconciliation and watchdog

PRD §26 roadmap row: *Engine API, MT5 adapter, reconciliation, watchdog EA.
Exit gate: failure-injection suite passes on demo.*

**Built ahead of the PRD's order.** §26 says not to build the engine until T0
finds an edge. The owner chose to build it while T0's data download runs
(2026-09-23). So T4 is strategy-agnostic: it contains no strategy and doesn't
depend on T0's results. It trades only what `config/strategies/` enables, and
that directory doesn't exist yet. With no strategy configured, the engine
monitors, reconciles and serves the operations API, and never opens a trade.

## Status

| | |
| --- | --- |
| Failure-injection suite, fake MT5 terminal | **38 of 38 pass.** Run `python tests/engine/t4_gate.py`. |
| Failure-injection suite, demo account | **Not run.** It needs the Windows VPS. See "Still owed". |
| Watchdog EA | Written but **not compiled**. MetaEditor is Windows-only. |
| Unit tests | 35 in `tests/engine/test_t4_units.py`. The full suite passes except the 2 dashboard failures already on `main` (see below). |
| Broker, credentials, orders | None. Nothing in this phase connected to a broker or placed an order, demo included. |

## What was built

| Piece | Where |
| --- | --- |
| Broker records in UTC; the engine never sees MT5 types or server time | `atlas_engine/adapters/broker.py` |
| Server time to UTC. Defaults to New York + 7 h (UTC+2 winter, UTC+3 summer). | `atlas_engine/adapters/mt5/clock.py` |
| MT5 adapter: the only code that calls `MetaTrader5`. It builds `ContractSpec` from `symbol_info()`, including `trade_tick_value_loss`, which was T3's open item. | `atlas_engine/adapters/mt5/adapter.py` |
| Fake MT5 terminal for tests and drills. It has the package's call names and constants, checked against `MetaTrader5` 5.0.6180, plus 6 injectable order faults and the terminal/broker switches. | `atlas_engine/adapters/mt5/fake.py` |
| Execution rules (§18, §21) | `atlas_engine/execution/` |
| Execution and filter settings (§27) with the PRD defaults | `atlas_engine/execution/settings.py` |
| Position book | `atlas_engine/positions/` |
| Reconciliation rules | `atlas_engine/reconciliation/` |
| Journal: the §24 tables plus restart state, in SQLite | `atlas_engine/journal/` |
| Engine alerts that don't go through Hermes (outbox file plus optional SMTP) | `atlas_engine/alerts/` |
| Signed operator commands (§11 layer 4) | `atlas_engine/operator.py` |
| Signal sources: runs a setup from the shared library on the broker's M1 bars | `atlas_engine/strategies.py` |
| The engine loop, which implements H3's operations interface | `atlas_engine/runtime.py` |
| `atlas-engine` command: run, token, operator, show | `atlas_api/engine_cli.py` |
| MQL5 watchdog EA | `watchdog/AtlasWatchdog.mq5` |
| Failure-injection scenarios, their test, the gate script and unit tests | `tests/engine/t4_*.py`, `tests/engine/test_t4_*.py` |

`atlas_engine/ops/health.py` (H3) gained one optional telemetry key,
`halt_reasons`. The engine uses it for HALT causes that H3 didn't list. Nothing
else from H3 or T3 changed.

## How it works

### One engine step (about once a second)

1. Beat its own heartbeat. Apply any signed operator commands in the inbox.
2. Read the terminal: connection, account, positions and one tick per symbol.
   Positions closed at the broker (SL, TP or by hand) are booked from deal
   history. The account goes to T3's `RiskEngine.observe`, and the engine
   flattens when the risk engine says so.
3. Reconcile at startup and then every 60 s.
4. Evaluate health with H3's state machine. KILL flattens and disables
   trading. HALT and DEGRADED only block new entries.
5. Poll the signal sources on closed bars, send what the risk engine allows,
   and apply the Friday 20:00 UTC flatten.
6. Save the restart state. A failed journal write is a HALT, and the engine
   won't send an order it can't journal.

### An entry

A signal's decision ID, `setup:version:symbol:bar close:direction`, hashes to
a 20-character client order ID. That ID goes in the order comment.

1. Health must allow the symbol and trading must be enabled.
2. T3's `check_entry` sizes the trade with the broker's contract spec and
   runs every T3 limit.
3. The intent is journaled *before* sending. If the engine crashes mid-order,
   the fill is adopted at restart instead of closed as an orphan.
4. The executor checks:
   - SL and TP are on the correct side and clear the stops level.
   - The spread is at most 20% of the stop distance.
   - The volume fits the symbol's step and limits, and the symbol's trade mode allows the direction.
5. It then sends one market order with SL, TP, magic and the client ID,
   deviation 2 points and the symbol's filling mode. `order_check` runs
   before every `order_send`.
6. When `order_send` gives no answer, it looks for the client ID in positions
   and deal history before any retry, and retries at most once. A requote
   retries once, only while the price is still within the deviation. Hard
   rejects never retry.
7. After a fill it verifies the SL at the broker. A missing SL is attached at
   once. If that fails the position is closed: ATLAS never holds an
   unprotected position.

Market orders match what the T0 backtester simulates. §21's "limit orders
preferred for pullbacks" should wait until a limit-entry variant is
backtested.

### Reconciliation

| Finding | Action |
| --- | --- |
| ATLAS position missing from the book, and the journal has its order | Adopt it |
| ATLAS position missing from the book, unknown order | Close it (`orphan_policy: close`) |
| Position ATLAS didn't open | Leave it alone. HALT until the operator deals with it. |
| In the book, gone at the broker | Book it closed from deal history. HALT if no closing deal is found. |
| SL missing or looser at the broker | Restore the book's SL. Close the position if that fails. |
| SL tighter at the broker | Adopt the broker's SL |
| TP differs | Restore the book's TP |
| Volume differs | Adopt the broker's volume |

Unresolved findings are the `mismatches` in H3's `reconciliation_report`. Any
unresolved finding is a HALT.

### Health (H3's thresholds, plus these HALT causes)

- `config_changed`: a config file's hash changed after load.
- `broker_trade_disabled`: the account or the terminal's algo trading is off.
- `request_budget`: 90% of FTMO's 2,000 server requests a day are used.
- `reconciliation_pending`: startup, before the first reconciliation.
- `firm_breached`

Three more rules:

- **Clock drift** is estimated from the freshest tick of the last 60 s, so a
  wrong server-time setting shows up as about an hour of drift, which is a
  HALT.
- **Losses:** the daily loss and drawdown fractions come from T3's own lines,
  so health's KILL fires exactly where T3's hard daily stop and drawdown stop
  sit.
- **Watchdog:** a silent watchdog heartbeat is a HALT; a FLATTENED heartbeat
  is a kill.

### The operator

Enabling trading, killing, flattening, clearing a kill and re-enabling after
the drawdown stop happen only through a command file signed with the
operator's key (HMAC-SHA256). The command must be under 120 s old and
carry an unused nonce. It is written into the engine's inbox on the engine
host:

```bash
atlas-engine operator keygen --key /srv/atlas/engine/operator.key
atlas-engine operator enable_trading --key /srv/atlas/engine/operator.key \
    --inbox /srv/atlas/engine/state/operator-inbox --operator yahye --reason "reviewed the incident"
```

The engine refuses to enable trading while HALT or KILL holds, or while a
manual kill is active. A fresh engine starts with trading disabled. A
restarted engine keeps its previous flag, but it sends nothing until the
startup reconciliation is clean.

The operations API is H3's exactly: five routes, and one write,
`disable_trading`. A test checks that no route can enable, flatten, kill or
touch risk, and that a read-only token gets 403 on the disable.

### Watchdog EA

The EA runs inside the terminal and doesn't need Python.

- It flattens and latches when equity reaches its own lines: 85% of the
  firm's daily loss and 80% of the firm's max loss. Those sit past the
  engine's KILL lines (75% and 60%) and inside the firm's floors.
- It takes the firm floors from `atlas_limits.txt`, which the engine rewrites
  every step. When that file is stale, it estimates them itself.
- It writes `ATLAS-WD 1 <utc> <equity> OK|FLATTENED` to Common\Files every 5 s.

## Decisions to confirm

- **The drawdown stop flattens.** H3's `health.py` treats 60% of the firm max
  loss as KILL, and the engine follows it. That is stricter than §19's
  "stop new trades", and T3's latch alone would only block entries.
- **After a KILL, the operator re-enables.** That includes the daily hard
  stop, which §19 lets resume the next server day.
- **Unknown ATLAS positions are closed**, not adopted with an attached SL.
  `execution.orphan_policy` can change it.
- **Foreign positions are never touched.** Any foreign position is a HALT,
  because the prop account is ATLAS-only.
- **The journal is SQLite on the engine host**, not the Postgres named in
  §24. Rows carry the §24 IDs and JSON, so a later copy to Postgres is
  straightforward.
- **Operator signatures are HMAC with a key on the engine host**, which needs
  no new dependency. Asymmetric signatures (the engine holding only a public
  key) would be better if the operator signs from another machine.
- **`config/atlas.yaml` has no `execution:` or `filters:` section yet.**
  The PRD §27 defaults apply, and `status` lists them under
  `settings_defaults_in_use`. Adding the sections is a signed operator commit.

## Running it

```bash
atlas-engine token --tokens /srv/atlas/engine/engine-tokens.yaml \
    --name operations-monitor/atlas-operations --scopes ops:read,ops:disable_trading
atlas-engine run --config config --state /srv/atlas/engine/state \
    --tokens /srv/atlas/engine/engine-tokens.yaml --operator-key /srv/atlas/engine/operator.key
atlas-engine show --state /srv/atlas/engine/state
```

- `--broker mt5` is the default. It needs Windows, the `MetaTrader5` package
  and a logged-in terminal. Credentials come only from the engine host's
  environment: `ATLAS_MT5_LOGIN`, `ATLAS_MT5_PASSWORD`, `ATLAS_MT5_SERVER`
  and `ATLAS_MT5_PATH`.
- `--broker fake` runs everything on the fake terminal for drills. It has no
  network and no broker.
- Point H3's installer at the engine with `--engine-url`, exactly as for the
  simulator.

## Still owed before T4 counts as done

1. **Pick the VPS** in the broker's region (open question 5: FTMO's MT5
   server, London by default), then install MT5 and Python on it.
2. **Compile `AtlasWatchdog.mq5`** in MetaEditor and attach it to a demo
   chart. Set `execution.watchdog_heartbeat_file` to the terminal's
   `Common\Files\atlas_watchdog.txt`.
3. **Confirm the broker's server time** with a tick versus UTC. Confirm the
   comment length the broker keeps, and that `trade_tick_value_loss` and
   filling modes are what the adapter expects.
4. **Run the failure-injection scenarios against the demo account**, with the
   operator present. The faults that the fake injects in code
   (`order_send` timeouts, dropped SLs) must be produced by hand there:
   - pull the network cable
   - kill the terminal
   - move an SL in the terminal
   - open a manual position
   - disable algo trading

   This is where orders would first be sent, and they go only to a demo
   account.
5. **Check the read-only config** on the Windows host. The check can't fail
   when running as root here.

## Deferred, and why

| What | Waits for |
| --- | --- |
| Any strategy trading | T0 (a passing setup), then a signed `config/strategies/*.yaml` |
| Exit management beyond fixed SL/TP and the Friday flatten. `move_stop` (tighten only, once per bar) is the hook. | T1 |
| Jev, calibration, EV gate; `decision_provider` reads `rules_only` | T2 |
| News blackout; no calendar feed yet | a news source |
| Weekly correlation matrix refresh; until then T3's shared-currency fallback applies | cron work |
| `atlas-emergency`, `execution-debugging` and `mt5-reconciliation` skills | H-track, after the demo run |
| `agent_actions` rows from Hermes hooks. The table exists; the hooks still write JSONL on the agent host. | H-track |

## Found on `main`, not fixed here

`tests/ops/test_dashboard.py` has 2 failures on `main` before this branch:

- `.gitignore`'s `dist/` rule kept the H3 dashboard tab's
  `atlas_plugins/hermes-plugin/dashboard/dist/` files out of the commit.
- The profile-env test needs Hermes importable.
