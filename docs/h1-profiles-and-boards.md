# Phase H1: profiles, boards, gateways and Docker backend

Built on 2026-09-22 against the pinned Hermes `v2026.9.21` (`d337b736`).
PRD roadmap row: *create 10 profiles as distributions, three Kanban boards,
gateway with allowlists, Docker backend. Exit gate: orchestrator routes a dummy
card through 3 profiles.*

## Status

| | |
| --- | --- |
| Exit gate, offline | **Passed.** 18 of 18 checks, see below. |
| Exit gate, live | Not run yet: needs a model key. `python tests/hermes/h1_gate.py --hermes <hermes> --live` |
| Static checks | 88 pass: `pytest tests/hermes/test_profiles.py` |
| Docker egress network | Written, not run here: this environment cannot pull container images. Run `deploy/hermes/egress/verify-egress.sh` on the agent host. |
| Telegram gateways | Configured, not connected: needs four bot tokens and the operator's Telegram id. |

## What was built

| Path | What it is |
| --- | --- |
| `atlas-profiles/<role>/` | Ten Hermes profile distributions (PRD §3): `distribution.yaml`, `SOUL.md`, `config.yaml`, `.gitignore`. Installable with `hermes profile install`. |
| `atlas-profiles/roster.yaml` | Tier, boards, bot platform and toolset allowlist per role. The tests hold every `config.yaml` to it. |
| `deploy/hermes/bootstrap.py` | Idempotent installer: installs the ten distributions, writes each model from its tier, applies the routing descriptions, creates the three boards. |
| `deploy/hermes/models.yaml` | Model per tier. One file to change models. |
| `deploy/hermes/boards.yaml` | `atlas-research`, `atlas-engineering` (worktrees of this repo), `atlas-ops`, with tenants `paper`, `eval-ftmo`, `funded-ftmo`. |
| `deploy/hermes/managed/config.yaml` | Host-wide Managed Scope policy (Docker network, approvals, disabled toolsets, gateway lockdown). |
| `deploy/hermes/egress/` | Internal Docker network `atlas-agents` whose only exit is an allowlisting squid proxy, plus a check script. |
| `deploy/hermes/systemd/` | Gateway unit template (one per bot profile) and the environment-file example. |
| `tests/hermes/` | `test_profiles.py` (static) and `h1_gate.py` (the exit gate). |
| `AGENTS.md` | Short rules that engineering workers load from their worktree (PRD §5.6). |

### Roles

| Profile | Tier | Toolsets | Bot |
| --- | --- | --- | --- |
| `atlas-orchestrator` | frontier | kanban, memory, skills | Telegram DM, hosts the dispatcher |
| `market-researcher` | cheap | web, skills, memory, session_search, todo | |
| `strategy-researcher` | mid | skills, memory, session_search, todo, code_execution | |
| `backtest-engineer` | mid | terminal, file, code_execution, skills, memory, session_search, todo | |
| `risk-analyst` | mid | skills, memory, session_search, todo, code_execution | Telegram |
| `execution-engineer` | mid | terminal, file, code_execution, skills, memory, session_search, todo | |
| `performance-analyst` | cheap | skills, memory, session_search, todo, code_execution | Telegram |
| `data-engineer` | cheap | terminal, file, code_execution, skills, memory, session_search, todo | |
| `jev-analyst` | mid | skills, memory, session_search, todo, code_execution | |
| `operations-monitor` | cheap | skills, memory, session_search, todo | Telegram |

Kanban workers also get the `kanban_*` tools. The ATLAS MCP servers and skills
arrive in H2 and will be added to these allowlists then. Default models are
`claude-opus-5`, `claude-sonnet-5` and `claude-haiku-4-5-20251001` for the
frontier, mid and cheap tiers; change them in `deploy/hermes/models.yaml`.

## Exit gate

`tests/hermes/h1_gate.py` builds a fresh Hermes home with the bootstrap, drops a
triage card on `atlas-research` as `atlas-orchestrator`, decomposes it, and runs
the dispatcher until the card graph closes.

Offline mode points every profile at a scripted model endpoint inside the test,
so the decomposer's routing choice is scripted and the run proves the wiring,
not the orchestrator model's judgment. The live mode checks the judgment with
real models. Offline result on 2026-09-22:

```text
PASS  decomposer fanned the card out to 3+ child cards          3 children
PASS  children went to 3+ distinct ATLAS specialists            market-researcher, strategy-researcher, risk-analyst
PASS  every child card is done                                  market-researcher=done, strategy-researcher=done, risk-analyst=done
PASS  parent card is owned by atlas-orchestrator                atlas-orchestrator
PASS  parent card was closed after the children                 done
PASS  orchestrator ran the parent card itself                   1 run(s) on the parent
PASS  board isolation: atlas-engineering is untouched           0 cards
PASS  board isolation: atlas-ops is untouched                   0 cards
PASS  decomposer saw all 10 ATLAS profiles
PASS  decomposer saw every routing description
PASS  each specialist ran under its own profile (SOUL loaded)   atlas-orchestrator, market-researcher, risk-analyst, strategy-researcher
PASS  orchestrator woke up to judge the parent
PASS  market-researcher was offered only its toolsets           kanban_* + memory, skill_view, skills_list, web_extract, web_search
PASS  strategy-researcher was offered only its toolsets         kanban_* + execute_code, memory, skill_view, skills_list
PASS  risk-analyst was offered only its toolsets                kanban_* + execute_code, memory, skill_view, skills_list
PASS  atlas-orchestrator was offered only its toolsets          kanban_* + memory, skill_view, skills_list
PASS  strategy-researcher received market-researcher's handoff
PASS  risk-analyst received strategy-researcher's handoff

H1 exit gate (offline): PASSED
```

The gate runs with `HERMES_MANAGED_DIR` pointing at `deploy/hermes/managed/`,
so the host policy is in force. Separately checked: with that policy active,
`hermes -p market-researcher config set approvals.cron_mode approve` is refused
as managed.

## Decisions and findings

**One host-wide policy, not one per profile.** H0 gap G1 planned one
`HERMES_MANAGED_DIR` per profile service. In practice one dispatcher (in the
`atlas-orchestrator` gateway) spawns every Kanban worker, and workers inherit
its environment, so a per-profile managed directory would not reach them.
Rules that hold for every profile are pinned once in
`deploy/hermes/managed/config.yaml`; per-role rules (toolset allowlists) are
in each profile's `config.yaml`, which the bootstrap owns. PRD §27 is updated.

**Per-role toolsets are allowlists.** Each profile sets `platform_toolsets` for
`cli` (which Kanban workers use), `cron` and its bot platform. The managed
policy removes browser, computer use, cron-job creation and other toolsets for
everyone. Only the three engineering roles get `terminal` and `file`
(PRD §11), and only they get their card's worktree mounted into the sandbox at
`/workspace`. `delegation` is off for all roles for now to bound spend; add it
per role in the roster when a card type needs it.

**One gateway per bot, one dispatcher.** `gateway.multiplex_profiles` is pinned
off so each bot runs as its own service (PRD §9, H0 gap G4). Only
`atlas-orchestrator` dispatches and delivers Kanban notifications; the other
gateways have both turned off.

**Allowlists only.** Each bot profile requires `TELEGRAM_ALLOWED_USERS`
(the operator's id), and the managed policy pins `gateway.allow_all_users:
false`. With no allowlist Hermes denies everyone. Per the owner's decision, a
Telegram message never authorizes a live-money action; every SOUL says so.

**Sandbox egress.** Sandboxes join the internal network `atlas-agents`, whose
only exit is the `atlas-egress` proxy allowing PyPI, GitHub and Dukascopy.
Model calls, web search and Telegram run in the Hermes processes on the host,
so sandboxes never need those hosts or any provider key. Hermes' own
credential-injecting egress proxy (`hermes egress`) is an alternative if
sandboxes ever need a provider key.

## Install on the agent host

1. Clone with `git clone --recurse-submodules --shallow-submodules` and install
   Hermes from `vendor/hermes-agent` into a venv.
2. Copy `deploy/hermes/managed/` to a root-owned `/etc/atlas/hermes-managed/`.
3. Give the service user Docker access and run
   `deploy/hermes/egress/setup-network.sh`, then `verify-egress.sh`. Membership
   of the `docker` group is root-equivalent; rootless Docker for the service
   user is safer.
4. `python deploy/hermes/bootstrap.py --hermes /srv/atlas/venv/bin/hermes --hermes-home /srv/atlas/hermes --repo /srv/atlas/repo`
5. For each bot profile, create a Telegram bot and fill `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_ALLOWED_USERS` in `/srv/atlas/hermes/profiles/<profile>/.env`.
6. Put the model key in `/etc/atlas/hermes.env` (see `systemd/hermes.env.example`).
7. Install `systemd/atlas-hermes-gateway@.service` and start
   `atlas-orchestrator` first, then the other three bots.
8. Run the live gate once: `python tests/hermes/h1_gate.py --hermes /srv/atlas/venv/bin/hermes --live`.

## Not verified here

- The egress network and proxy (image pulls are blocked in this environment).
- Gateways against real Telegram bots.
- Whether `HERMES_WRITE_SAFE_ROOT` set on the gateway unit reaches the workers
  it spawns. Check on the host before relying on it; config ownership by the
  engine's OS user (PRD §12) stays the real guard for `config/`.
- Routing by the real orchestrator model (the live gate).
