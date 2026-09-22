# Open questions: decisions

Sep 22, 2026. Answers the "Open questions" list at the end of `ATLAS_v3_Hermes_Derived_PRD.md`. Questions 3 and 7 were answered by the owner; the others are Claude's recommendations, not yet overridden by the owner.

| # | Question | Proposed decision | Blocks |
| --- | --- | --- | --- |
| 1 | Hermes pin | Settled: `v2026.9.21` (`d337b736`). See `docs/hermes-h0-checklist.md`. | — |
| 2 | Prop firm, account size, EAs allowed? | FTMO 2-Step, $10k for the first evaluation. EAs are allowed. | T3, T8 |
| 3 | What is Jev? | Owner: not built yet. A future ATLAS decision component, built in T2 after the gradient-boosted baseline and tested as its own arm. Backed by TypeSafe AI's Jev API. | T2 |
| 4 | M15 or H1? | M15 decision bars with an H1 trend filter (intraday). | T0 |
| 5 | Broker server and VPS region | FTMO's MT5 server. Pick the VPS region by measured ping (< 20 ms) from London and New York trial VPSes; default London. | T4 |
| 6 | Live agent trade intents? | No. Paper/demo only for all of v3; revisit only after T9. | — |
| 7 | Operator and approval channel | Owner: sole operator. Telegram DM for alerts, blocked trades, incidents and promotion requests; a Telegram reply never authorizes anything. Approvals use the signed operator mechanism. | H3, T4 |

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

Owner decision: Jev is **not built yet**. It is the name of a future specialized decision component/API in ATLAS. No part of the build may assume an existing ATLAS Jev model or prompt.

Order in T2:

1. Rules-only arm (the setups with no selection layer).
2. Gradient-boosted baseline arm.
3. Jev arm, implemented after the baseline and tested against both on identical data (§17 keep/kill: Jev stays only if it beats both out of sample with ≥ 300 trades per arm).

The owner described the model the component will call: TypeSafe AI's Jev, a non-generative "System One" model launched Sep 15, 2026. Its published docs confirm: text/JSON input only; typed outputs with probabilities from choice, score and yes/no questions; `POST /v1/systemone` with default alias `jev-latest`; vendor-stated 70–500 ms end-to-end latency; $0.042 per million input tokens, output free ([docs](https://docs.typesafe.ai/concepts/system-one), [launch post](https://typesafe.ai/blog/introducing-system-one-models-and-jev)).

How that fits §17:

- `p_target_first` = a yes/no question ("does price reach the target before the stop?"); `regime` = a choice question over ATLAS' fixed regime labels. The compact state is sent as JSON text.
- **Calibration stays.** The vendor's calibration is measured on its own tasks, not on ATLAS trades, so the isotonic refit and Brier checks in §17 still apply.
- **Latency is the tight spot.** The vendor's upper figure (500 ms) equals ATLAS' timeout, and the DEGRADED state trips at p95 > 500 ms. T2 measures latency from the trading host before committing to the budget.
- **Leakage risk.** A pretrained model may have seen 2019–2026 market history. States sent to Jev must carry no absolute dates, timestamps, price levels or news text, only normalized features. Backtest results for the Jev arm count as optimistic until paper trading (T7) confirms them.
- **Determinism.** Pin a specific model version rather than `jev-latest` for live use (if TypeSafe offers versioned aliases; not confirmed in its docs), pin question wording, cache by state hash. A model or wording change is a new strategy version.
- **Cost is negligible.** At $0.042 per million input tokens, replaying 20,000 historical setups at 2,000 tokens each costs about $1.70.

## 4. Timeframe: M15 with H1 filter

- The setup library (§16) and exits (§18) are already written for M15 entries with H1 context (ATR(M15) stops, 12 × M15 time stop, H1 invalidation).
- The ≥ 300 out-of-sample trades gate (§15) is hard to meet on H1 swing: the validation window (Jan 2024–Jun 2025) is about 78 weeks, and two symbols at 1–2 swing trades per week each gives roughly 150–300 trades. M15 clears it comfortably.
- H1 swing means holding overnight and over weekends, which conflicts with the Friday flatten and FTMO's gap-trading rule.

## 5. Broker server and VPS region

The broker is whatever MT5 server FTMO assigns. Before T4, rent short-term Windows VPSes in London (LD4) and New York (NY4), log into the FTMO MT5 terminal from each, and keep the one under the 20 ms ping target (§21). London is the default because EURUSD and GBPUSD liquidity centres there; this is an inference, not a measurement.

## 6. Live agent trade intents

No for v3, as §11 already argues. Revisit per strategy only after T9, through the promotion gate and operator signature.

## 7. Operator and approvals

Owner decision: the owner is the **sole human operator**.

- **Telegram DM** carries alerts, blocked trades, incidents and promotion requests (§9).
- **A Telegram reply never authorizes anything.** Live enablement, risk changes, strategy promotion and emergency flattening go only through the signed operator mechanism outside Hermes (§11 layer 4): a signed commit to `config/` or a signed operator CLI command.
- Kill-switch and hard-limit alerts are also sent by the engine directly by email and SMS, so a gateway outage never silences them.
