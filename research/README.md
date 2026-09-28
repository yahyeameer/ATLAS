# Research records

- `experiments.jsonl` — append-only registry of every T0 and T1 experiment
  (T1 entries carry `kind: exit_research`), passes and failures alike. The
  deflated Sharpe ratio counts the trials recorded here, so never delete or
  edit entries. Budget: 20 experiments per strategy per month.
- `runs/` — per-run reports and trade lists (not committed; regenerate from the
  registry's parameters and data window).

See `docs/t0-edge-discovery.md` and `docs/t1-exit-research.md`.
