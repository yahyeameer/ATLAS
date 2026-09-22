# ATLAS profiles

One Hermes profile distribution per ATLAS role (PRD §3). `roster.yaml` holds
each role's tier, boards, bot platform and toolset allowlist.

Install all of them, with models and boards, using the bootstrap:

    python deploy/hermes/bootstrap.py --hermes <path to hermes>

A single profile can also be installed on its own with
`hermes profile install atlas-profiles/<role>`, but then its model and routing
description are not set. See `docs/h1-profiles-and-boards.md`.
