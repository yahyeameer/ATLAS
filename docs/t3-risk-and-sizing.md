# Phase T3: risk engine, prop rules and sizing

PRD §26: risk engine, prop YAML, netting, full tests. Exit gate: **100% of
the risk tests pass, and the breach probability is under 2%.**

T3 is the one trading-core phase that doesn't depend on T0's outcome. Firm
limits, internal loss limits, lot sizing and currency netting are the same
whatever strategy trades on top of them. Nothing here places an order,
talks to MT5 or reads market data. That comes in T4, and only if T0 finds
an edge.

## What was built

| Piece | Where |
| --- | --- |
| FTMO 2-Step rules as data, re-checked against FTMO's pages on 2026-09-23 | `config/prop_rules/ftmo_2step.yaml` |
| Internal limits (PRD §19, §20, §27 defaults) | `config/risk.yaml` |
| Engine config: mode, phase, account, symbols | `config/atlas.yaml` |
| Strict loader: unknown keys, out-of-range values and internal limits outside the firm's are refused; files are fingerprinted; optional read-only check for the engine host | `atlas_engine/config.py` |
| Prop-rule arithmetic: server day (Prague midnight, DST-aware), daily and max-loss floors, static and trailing drawdown, news window, phase targets | `atlas_engine/prop_rules/` |
| Risk engine: account latches, per-trade checks, journal-ready decisions, state that survives a restart | `atlas_engine/risk/` |
| Lot sizing (§20 formula, volume step, min/max, prop max lot, 110% rule, commission counted in the risk) | `atlas_engine/sizing/` |
| Currency legs, net risk per currency, correlation clusters with a conservative fallback | `atlas_engine/exposure/` |
| Prop evaluation Monte Carlo through the engine | `atlas_research/prop_sim.py`, `atlas-research prop-mc` |
| Tests (126) and the exit-gate script | `tests/engine/` |

## FTMO 2-Step rules as encoded

Sources: [Trading Objectives](https://ftmo.com/en/trading-objectives/),
[Can I trade news?](https://ftmo.com/en/faq/can-i-trade-news/),
[Overnight and weekend](https://ftmo.com/en/faq/do-i-have-to-close-my-positions-overnight-or-before-the-weekend/).

- **Maximum daily loss** is 5% of initial capital. It is measured from the
  day's starting balance at 00:00 CE(S)T, with floating P/L, swaps and
  commissions included. On $10k, equity may not fall below
  `day-start balance − $500`.
- **Maximum loss** is 10% of initial capital and static, so equity may never
  fall below $9,000.
- **Targets** are 10% in the challenge and 5% in verification. Each phase
  needs at least 4 trading days, where a trading day is one on which a
  position is opened.
- **News and weekend rules** don't apply during the evaluation, on either
  account type. On a funded Standard account, no trade may open or close
  (SL/TP included) from 2 minutes before to 2 minutes after a restricted
  release, and positions must be closed before the weekend or any market
  break longer than 2 hours.
- EAs are allowed, up to 2,000 server requests a day.

The loader refuses a firm whose YAML says EAs aren't allowed (PRD §19).

## Internal limits

The equity levels on a fresh $10k FTMO account:

| Limit (PRD §19) | Rule | Level on $10k | Action |
| --- | --- | --- | --- |
| Daily soft stop | 50% of firm daily loss | 9,750 | No new trades until the next server day |
| Daily hard stop | 75% of firm daily loss | 9,625 | Flatten; no new trades until the next server day |
| Drawdown stop | 60% of firm max loss | 9,400 | No new trades until the operator re-enables |
| Drawdown halving (§20 m) | 50% of the internal drawdown limit | 9,700 | Risk × 0.5 |
| Open risk | ≤ 1.5% of equity to stops | | Deny the entry |
| Net risk per currency | ≤ 1.0% of equity | | Deny an entry that grows it past the cap |
| Correlated positions | Direction-adjusted 60-day correlation > 0.7 counts as one bet | one trade's risk | Deny |
| Trades per day | 6 | | Deny |
| Loss streak | 4 losing trades in a row | | Risk × 0.5 for the next 10 trades |
| Firm headroom | Equity minus every open stop minus this one must stay above both firm floors | | Deny |

The internal daily loss counts from the higher of day-start balance and
day-start equity. Floating profit carried overnight is protected too, which
is stricter than FTMO.

The loader also checks two invariants that the PRD implies but doesn't
state. Once the soft stop blocks new trades, the worst the rest of the day
can do is the open risk to stops. So `soft share × firm daily loss + max
open risk` must be under the firm daily loss (2.5% + 1.5% < 5%). The same
check applies to the drawdown stop against the max loss (6% + 1.5% < 10%).
Risk per trade above 0.50% is refused outside evaluation mode, and above
0.75% everywhere.

## Exit gate result

`python tests/engine/t3_gate.py --sims 10000` **passes**. All 126 risk
tests pass (1 is skipped: the read-only file check can't fail when running
as root, so a stubbed version covers it). With the engine, the chance of
breaching a firm rule within 60 trading days is 0.00% on every profile, and
the gate needs under 2%:

| Risk per trade | Profile (expectancy) | Breach, engine | Breach, firm rules only | Pass in 60 days, engine / firm only | Internal drawdown stop |
| --- | --- | --- | --- | --- | --- |
| 0.40% | edge (+0.20R) | 0.00% | 0.24% | 20.9% / 36.8% | 0.3% |
| 0.40% | breakeven | 0.00% | 5.45% | 2.2% / 4.5% | 5.8% |
| 0.40% | losing (−0.15R) | 0.00% | 19.92% | 0.3% / 0.6% | 19.7% |
| 0.40% | busy breakeven (4 trades/day) | 0.00% | 25.79% | 8.3% / 22.3% | 24.8% |
| 0.75% | edge (+0.20R) | 0.00% | 5.28% | 57.8% / 72.2% | 7.9% |
| 0.75% | breakeven | 0.00% | 32.74% | 16.9% / 24.9% | 37.3% |
| 0.75% | losing (−0.15R) | 0.00% | 63.09% | 4.6% / 7.0% | 65.7% |
| 0.75% | busy breakeven | 0.00% | 63.86% | 25.3% / 33.5% | 60.1% |

The engine turns breaches into internal drawdown stops. The account is
paused at −6% for the operator to review instead of being failed at −10%.

The breach numbers come from synthetic trade lists with a known expectancy,
not from market data. They show that the limits hold across edge, breakeven,
losing and high-frequency profiles. They don't say anything about a real
strategy.

How the Monte Carlo works, and why it is pessimistic:

- It resamples whole trading days, so clustering within a day is kept.
  Trades keep their time of day, and they can overlap.
- At every exit, every open trade is assumed to sit at its worst excursion
  (MAE) at the same moment. A daily hard stop closes all open trades at
  their MAE, not at the stop line.
- Losers gap through the stop 5% of the time, losing 1.2 to 2R.

## Finding: the PRD's internal limits cost pass speed

The loss-streak halving and the correlated one-bet rule aren't needed to
keep the breach rate under 2% on these profiles, and both slow the
challenge down. The drawdown halving makes little difference either way.
Here is a 1,500-path ablation on the +0.20R profile over a 60-day horizon:

| Limits on | P(pass in 60 days) | P(firm breach) |
| --- | --- | --- |
| All (PRD defaults) | 21.9% | 0.00% |
| Without the loss-streak halving | 31.5% | 0.00% |
| Without the correlated one-bet rule | 26.6% | 0.00% |
| Without both halvings and the one-bet rule | 39.3% | 0.00% |
| No engine, firm rules only | 39.2% | 0.20% |

These are the PRD's defaults, so they stay as they are. Revisit them with
real out-of-sample trades once T0 has a candidate: run
`atlas-research prop-mc --trades <oos_trades.csv>` with and without each
rule. Changing them is a signed operator commit to `config/risk.yaml`.

## Using it

```bash
atlas-research prop-mc --reference breakeven             # synthetic profile
atlas-research prop-mc --trades research/runs/<id>/oos_trades.csv --sims 10000
python tests/engine/t3_gate.py --sims 10000 --out t3_gate.json
pytest tests/engine
```

`prop-mc` refuses a trade list that reaches the locked holdout.

In T4 the engine loop will make these calls, in order:

1. `RiskEngine(load_engine_config("config", require_read_only=True), state)`.
   Persist `engine.state.to_dict()` after every change and restore it on
   restart.
2. `observe(AccountSnapshot)` on every equity update. Act on
   `Assessment.flatten` and `new_trades_allowed`.
3. `check_entry(TradeProposal, snapshot, correlation)` before every order.
   Journal `RiskDecision.to_dict()` to `risk_checks`.
4. `record_open` and `record_close` as fills arrive.
5. Drain `engine.events` into `system_events`.
6. Call `operator_reenable` only after the engine API has verified the
   operator's signature (PRD §11 layer 4). No MCP tool reaches it.

## Not done here, and why

- **No MCP tool for the prop Monte Carlo yet.** The `prop-rule-analysis`
  skill (§7) needs one on `atlas-backtest`. That is H-track work. For now
  the `risk-review` skill points at the CLI.
- **T0's gates still use `metrics.daily_breach_probability`** (closed P/L,
  fixed risk). T0 is running in its own thread, so changing its gates
  mid-run would be bad. Switching them to `prop_sim` is a one-line follow-up
  once T0 has results.
- **Contract specs are a static table** for EURUSD, GBPUSD, USDJPY and
  XAUUSD, with the $7/lot commission placeholder. T4 fills `ContractSpec`
  from MT5 `symbol_info()`, including `trade_tick_value_loss`.
- **The weekly correlation matrix** is computed by `correlation_matrix()`
  from daily closes. Scheduling it is H3/T4 cron work. Until a fresh matrix
  exists, the engine treats any two positions with a shared currency leg
  pointing the same way as one bet.
- **The read-only config check** was tested with a stub, because the cloud
  environment runs as root. Check it on the Windows engine host in T4.
- **The config files were written by Claude, not signed by the operator.**
  AGENTS.md says `config/risk*` and `config/prop_rules/` change only by a
  signed operator commit. These are first versions for the owner to review
  in this PR. Merging them is the operator's approval, and later changes
  should be signed.
