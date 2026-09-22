# Phase H0: Hermes fork, pin and capability checklist

Checked on 2026-09-22 against upstream `NousResearch/hermes-agent` (MIT licence).
Method: review of the source tree and bundled docs (`website/docs/`) at the pinned
tag, plus a scratch install of the tag (Python 3.11) where the Kanban items were
exercised from the CLI: a named board, a tenant, a goal-mode card with a turn cap and
model override, a duplicate create deduplicated by idempotency key, and a swarm graph
(two workers, verifier, synthesizer). No agent turns were run (no model keys), so
dispatch, hooks, gateway and approvals are verified from code and docs only.

## Pin

| | |
| --- | --- |
| Upstream | https://github.com/NousResearch/hermes-agent |
| Tag | `v2026.9.21` |
| Commit | `d337b736aa1e8ebecfab043842d13e4a2d2f48a3` (2026-09-21) |
| Package version | `0.21.4` |
| Oldest acceptable tag | `v2026.9.7` (first tag with `hermes kanban swarm`) |

Why this tag: it is the newest release tag, and every capability ATLAS relies on is
present in it. Upstream moves very fast (about 5,000 commits between `v2026.9.14` and
`v2026.9.21`, and about 450 more on `main` since), so ATLAS pins a release tag, never
`main`, and bumps the pin deliberately with this checklist re-run each time.

### Proposed fork layout

§25 of the PRD describes ATLAS as the Hermes tree with ATLAS directories added on top.
With upstream at ~15,000 files and ~5,000 commits a week, merging upstream into the
ATLAS tree would be a constant conflict source. Everything ATLAS adds (profiles,
skills, MCP servers, plugins, hooks, dashboard tab) loads from outside Hermes core, so
the proposal is:

1. Fork `NousResearch/hermes-agent` to `yahyeameer/hermes-agent`. Create branch
   `atlas/v2026.9.21` from the tag. Core patches, if any are ever needed, go there
   and are upstreamed.
2. Add that fork to ATLAS as a git submodule at `vendor/hermes-agent`, checked out at
   the pinned commit. The submodule SHA is the pin.
3. Keep all ATLAS code in this repo (`atlas_engine/`, `atlas_mcp/`, `atlas_plugins/`,
   `atlas-profiles/`, `atlas-skills/` and the rest of §25), installed into Hermes as
   plugins, profile distributions and MCP config.

## Capability checklist (PRD §2)

| # | Capability | At `v2026.9.21` | Evidence |
| --- | --- | --- | --- |
| 1 | Profiles, `--description` for routing | Present | `hermes profile create --description`, `hermes profile describe` (docs `features/kanban.md`) |
| 2 | Persistent memory (`MEMORY.md`, `USER.md`, session search, external providers) | Present | `plugins/memory/*`, `hermes_state_search.py` |
| 3 | Skills, agent-created skills, Curator | Present | `skills/`, `agent/learning_mutations.py` |
| 4 | MCP with tool `include`/`exclude` and filtered subprocess env | Present | `reference/mcp-config-reference.md`; `tools/mcp_tool_transport.py` |
| 5 | Toolsets, `agent.disabled_toolsets` | Present | `toolsets.py`, `agent/agent_init.py` |
| 6 | `delegate_task` | Present | `agent/tool_guardrails.py` |
| 7 | Kanban: boards, tenants, `kanban_complete(summary, metadata)`, `kanban_request_changes`, `kanban_block(kind=needs_input)`, retries, crash reclaim, circuit breaker, idempotency keys, `scheduled_at`, per-task model override, `max_in_progress`, subscriptions | Present | `hermes_cli/kanban_*`, `gateway/kanban_watchers_*`, docs `features/kanban.md` |
| 7a | Kanban goal-mode cards (`--goal`, `--goal-max-turns`) | Present | docs `features/kanban.md` "Goal-mode cards" |
| 7b | Kanban swarm (`hermes kanban swarm`: workers, verifier, synthesizer) | Present | `hermes_cli/kanban_parser.py` "Kanban Swarm v1" |
| 8 | Bot Mode | Present, see gap G4 | docs `user-guide/bot-mode.md` |
| 9 | Messaging gateway, allowlists, deny-all default, `hermes send` | Present | docs `security.md` "User Authorization"; `guides/pipe-script-output.md` |
| 10 | Cron, script-only jobs (`no_agent=true`) | Present | `cron/`, `guides/cron-script-only.md` |
| 11 | Context files (`AGENTS.md`, `.hermes.md`, `SOUL.md`) with injection scanning | Present | `hermes_cli/config_defaults.py`, docs `security.md` |
| 12 | Config, Managed Scope | Present, see gap G1 | `hermes_cli/managed_scope.py`, docs `managed-scope.md` |
| 13 | Providers: fallback, credential pools, per-profile model | Present | `agent/turn_api_error.py`, `credential_pool` |
| 14 | `execute_code` | Present | `tools/`, `agent/tool_guardrails.py` |
| 15 | Terminal backends: local, Docker, SSH, Daytona, Singularity, Modal, Vercel Sandbox | Present | `tools/environments/` |
| 16 | Approvals: `mode`, `cron_mode: deny`, `unattended_mode: deny`, `approvals.deny`, hardline blocklist, write denylist | Present, see gap G2 | `hermes_cli/config_defaults.py` (`approvals`), docs `security.md` |
| 17 | Egress controls | Partial, see gap G3 | `docker_network`, `website_blocklist`, SSRF guard |
| 18 | Secrets: Bitwarden, 1Password, command helper | Present | docs `security-1password`, `bitwarden` sources |
| 19 | Hooks: lifecycle, Kanban `task_claimed/completed/blocked`, observer hooks, plugins | Present, see gap G6 | docs `features/hooks.md`, `developer-guide/observer-hooks.md` |
| 20 | Dashboard with plugin tabs (Kanban ships as one), localhost bind | Present | `plugins/kanban/dashboard/`, docs `features/kanban.md` |
| 21 | Batch processing / trajectories | Present | `batch_runner.py`, `trajectory_compressor.py` |

## Gaps and decisions (patch vs plugin vs config)

None of the gaps needs a Hermes core patch. Each is closed by configuration or deployment.

**G1. Managed Scope is per machine, not per profile, and advisory against root.**
It reads one directory (`/etc/hermes`, or `HERMES_MANAGED_DIR`). Enforcement is
filesystem permissions only, the managed `.env` is world-readable, and the agent can
still override a managed env value inside its own subprocess shell.
*Decision: config/deploy.* Run each profile's gateway/worker as a non-root service
with its own `HERMES_MANAGED_DIR` fixed in the service unit, root-owned policy
files, and no secrets in the managed `.env`. Treat Managed Scope as convenience;
engine token scopes (PRD §6) stay the real boundary.

**G2. `approvals.deny` only matches terminal commands.**
The PRD's `"*config/risk*"` rule will not stop `write_file` or `patch`. The built-in
write denylist is fixed (credential and system paths) and cannot be extended.
*Decision: config/deploy.* Set `HERMES_WRITE_SAFE_ROOT` to each profile's workspace
so file tools cannot write outside it, and keep `config/` owned by the engine OS user
(PRD §12 already requires this). Keep the `approvals.deny` rules for shell commands.

**G3. No outbound egress allowlist.**
Hermes offers `terminal.docker_network` (on/off), `docker_extra_args` (so a custom
Docker network can be passed), a website blocklist and an SSRF guard, but no
allowlist of destinations.
*Decision: deploy.* In H1, create a dedicated Docker network whose outbound traffic
goes through a filtering proxy or host firewall that allows only model providers,
the MCP/engine host and approved data sources.

**G4. Bot Mode is a desktop-app UI.**
A Bot is a profile; Bot Mode is the roster view in the desktop app. On the headless
Linux host the same result comes from per-profile gateways (`multi-profile-gateways.md`)
plus per-profile cron.
*Decision: config.* Use per-profile gateways for the four ATLAS bots; the desktop Bot
Mode view is optional for the operator.

**G5. Plugin import paths are unstable.**
The September 2026 decomposition moved internal modules. A compatibility shim
(`COMPAT_MANIFEST.md`) was due for removal on 2026-09-14 but is still present at
`v2026.9.21` and on `main`, so it will disappear in a coming release.
*Decision: plugin rule.* ATLAS plugins and hooks import only from the new locations,
and CI runs `hermes plugins compat atlas_plugins/` so a pin bump that breaks them
fails loudly.

**G6. Kanban hooks fire in different processes.**
`kanban_task_claimed` fires in the dispatcher; `kanban_task_completed` and
`kanban_task_blocked` fire in the worker.
*Decision: plugin.* Register the ATLAS Kanban-to-alert and audit hooks in the
dispatcher profile (observes all transitions); worker-side hooks only where needed.

**Noted, no action:** Kanban is single-host by design, which matches the PRD (one
Linux agent host). Kanban dashboard plugin routes are unauthenticated and must stay
bound to localhost, as the PRD already says.

## Open question answered

PRD open question 1: pin `v2026.9.21` (`d337b736`). It includes Managed Scope,
Kanban swarm and goal-mode cards.

## Next steps (need owner approval)

1. Create the `yahyeameer/hermes-agent` fork and the `atlas/v2026.9.21` branch.
2. Add it to ATLAS as the `vendor/hermes-agent` submodule and commit this checklist.
3. Update PRD §25 and the open-questions list to match.
