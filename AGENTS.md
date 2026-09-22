# ATLAS: rules for agents working in this repo

ATLAS is a Forex research and trading system on Hermes. The design is
`ATLAS_v3_Hermes_Derived_PRD.md`; decisions are in `docs/`.

- Stay inside your Kanban card and its git worktree. Don't push to `main`.
- Never read, request or load holdout data (PRD §22). Research code refuses it; don't work around that.
- Never edit `config/risk*`, `config/prop_rules/` or `config/strategies/`. Those change only by a signed operator commit.
- No code path may let an agent place live trades, change risk or enable trading (PRD §6, §11).
- Report every experiment you ran, failures included, in R after costs.
- Run the tests for what you changed before you hand off.
- Hermes is the pinned submodule `vendor/hermes-agent`. Don't patch it; ATLAS code loads from outside it.
