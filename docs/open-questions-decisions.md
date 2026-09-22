# Open questions: proposed decisions

Status: **proposed, awaiting owner confirmation** (Sep 22, 2026). Answers the "Open questions" list at the end of `ATLAS_v3_Hermes_Derived_PRD.md`. Items marked *owner input* are facts only the owner can supply; the default shown is what the build assumes until then.

| # | Question | Proposed decision | Blocks |
| --- | --- | --- | --- |
| 1 | Hermes pin | Settled: `v2026.9.21` (`d337b736`). See `docs/hermes-h0-checklist.md`. | — |
| 2 | Prop firm, account size, EAs allowed? | FTMO 2-Step, $10k for the first evaluation. EAs are allowed. | T3, T8 |
| 3 | What is Jev? | *Owner input.* Until known, treat Jev as a black box behind the §17 contract; T2 builds the gradient-boosted baseline first. | T2 |
| 4 | M15 or H1? | M15 decision bars with an H1 trend filter (intraday). | T0 |
| 5 | Broker server and VPS region | FTMO's MT5 server. Pick the VPS region by measured ping (< 20 ms) from London and New York trial VPSes; default London. | T4 |
| 6 | Live agent trade intents? | No. Paper/demo only for all of v3; revisit only after T9. | — |
| 7 | Operator and approval channel | *Owner input.* Default: the owner is the sole operator; Telegram DM for alerts and requests; approvals by signed commit or signed CLI outside Hermes; email + SMS as the independent alert path. | H3, T4 |

## 2. Prop firm: FTMO 2-Step, $10k

FTMO's published objectives for the 2-Step challenge ([Trading Objectives](https://ftmo.com/en/trading-objectives/)):

- Profit target 10% (phase 1), 5% (verification); none on the funded account.
- Maximum daily loss 5% of initial capital, reset at 00:00 CE(S)T from the day's opening balance.
- Maximum loss 10% of initial capital, **static** (the 1-Step's is end-of-day trailing and its daily loss is 3%, which is why 2-Step is preferred).
- Minimum 4 trading days per phase.

EAs are permitted; FTMO's own FAQ discusses third-party EAs and its forbidden-practices page limits EAs to at most 2,000 server requests per day ([FAQ](https://ftmo.com/en/faq/which-instruments-can-i-trade-and-what-strategies-am-i-allowed-to-use/), [Forbidden Trading Practices](https://ftmo.com/en/forbidden-trading-practices/)). ATLAS at ≤ 6 trades/day is far below that. Gap trading around major news and before market closes is forbidden; ATLAS' own news blackout and Friday flatten (§16, §18) already cover this. Copy trading and running one strategy across many accounts are restricted, so ATLAS runs one account per strategy.

Why $10k: sizing is a percentage of equity (§20), so account size changes only the fee and payout, not the system. The smallest account makes a failed first evaluation cheapest. At 0.25% risk, $10k gives $25 per trade, which on EURUSD with a 15-pip stop is about 0.16 lots, well above the 0.01 lot step. Scale up after the first pass.

Resulting `config/prop_rules/ftmo_2step.yaml` values: `daily_loss_pct: 5`, `daily_loss_basis: day_open_balance`, `daily_reset: "00:00 Europe/Prague"`, `max_loss_pct: 10`, `max_loss_type: static`, `min_trading_days: 4`, `eas_allowed: true`, `max_server_requests_per_day: 2000`. Re-check FTMO's pages when writing the YAML in T3; firms change terms.

## 3. Jev

The PRD defines Jev's interface (§17) but not what it is. Owner to say which applies:

- an LLM prompt that already exists,
- a trained classifier that already exists,
- a name for a selection model not built yet.

Nothing before T2 depends on the answer. If Jev does not exist yet, T2 builds the gradient-boosted baseline first and Jev becomes a later arm of the keep/kill experiment. Latency is measured in T2 against the 500 ms budget.

## 4. Timeframe: M15 with H1 filter

- The setup library (§16) and exits (§18) are already written for M15 entries with H1 context (ATR(M15) stops, 12 × M15 time stop, H1 invalidation).
- The ≥ 300 out-of-sample trades gate (§15) is hard to meet on H1 swing: the validation window (Jan 2024–Jun 2025) is about 78 weeks, and two symbols at 1–2 swing trades per week each gives roughly 150–300 trades. M15 clears it comfortably.
- H1 swing means holding overnight and over weekends, which conflicts with the Friday flatten and FTMO's gap-trading rule.

## 5. Broker server and VPS region

The broker is whatever MT5 server FTMO assigns. Before T4, rent short-term Windows VPSes in London (LD4) and New York (NY4), log into the FTMO MT5 terminal from each, and keep the one under the 20 ms ping target (§21). London is the default because EURUSD and GBPUSD liquidity centres there; this is an inference, not a measurement.

## 6. Live agent trade intents

No for v3, as §11 already argues. Revisit per strategy only after T9, through the promotion gate and operator signature.

## 7. Operator and approvals

Default until the owner says otherwise: the owner is the sole human operator. Telegram DM carries alerts, blocked cards and promotion requests (§9). Enabling trading, risk changes, promotion and flattening are approved outside Hermes by a signed commit to `config/` or a signed CLI command (§11 layer 4), never by a chat reply. Kill-switch and hard-limit alerts also go by email and SMS from the engine.
