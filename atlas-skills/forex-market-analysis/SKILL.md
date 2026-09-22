---
name: forex-market-analysis
description: "Describe current or historical Forex market conditions for ATLAS symbols: sessions, volatility, trend, spreads and event risk, with sources."
version: 0.1.0
author: ATLAS
license: UNLICENSED
platforms: [linux]
metadata:
  hermes:
    tags: [ATLAS, Forex, Market, Sessions]
    related_skills: [session-breakout-research, data-quality]
---

# Forex Market Analysis

## Objective

Give researchers a sourced, numeric picture of market conditions for EURUSD, GBPUSD and XAUUSD: volatility, trend state, spreads by session, and scheduled event risk. Facts and interpretation are kept apart, and every fact names where it came from.

## When to use

- A card asks about session behaviour, volatility regimes, spreads or broker conditions.
- A strategy researcher needs context before designing an experiment (for example, how wide spreads get in the New York rollover).
- An event calendar or central-bank schedule needs summarising for the research window.

Don't use for: trade ideas or signals (ATLAS trades only deterministic setups through the engine), or running backtests.

## Required inputs

- Symbols and the period of interest. The research data covers the dev and validation periods only; anything later is the locked holdout and is refused.
- The question on the card.

## Procedure

1. Get a compact state: `collect_market_state(symbols)` returns mid, M15 ATR in pips, its 60-day percentile, H1 trend and ADX, median spread and the prior-day range at the last research bar.
2. Get spreads by session: `get_spread_stats(symbol, window)` gives median and p95 spread in pips for Asia, London, overlap and New York. Compare against the setups' rule that spread must stay under 20% of the stop distance.
3. When a pattern needs bars, pull them sparingly: `get_bars(symbol, "H1", window, limit)` or `"M15"`; at most 500 bars per call. Prefer summaries over raw bars.
4. For events, broker conditions and news, use web search and cite each source with its date. Mark anything older than the question's period as possibly stale.
5. Separate what the data shows from what you think it means. Where the data can't answer the question, say so.

## Tools

- atlas-market: `collect_market_state`, `get_bars`, `get_spread_stats`
- web search for calendars, central-bank schedules and broker documentation

## Output format

A short note: question, findings as numbered facts (each with its tool call or URL), interpretation, open questions. Complete the card with `artifacts` listing the sources, and any `data_window` you used.

## Safety constraints

- Never request holdout data. Windows reaching past the validation period are refused.
- No trade calls, signals or price targets. ATLAS trades only through the engine's deterministic setups.
- Never put credentials or account details in notes.
- Report every source you relied on, including ones that contradicted your conclusion.
- Memory entries stating results must reference the tool call or source they came from.

## Validation requirements

- Every number traces to a tool result from this session or a cited source.
- Spreads are in pips and volatility in pips or ATR multiples, with the window stated.
- Interpretation is labelled as such.
