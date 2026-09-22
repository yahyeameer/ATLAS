"""Static checks on the ATLAS Hermes profiles, boards and host policy (phase H1).

These run without Hermes installed. The end-to-end exit gate is h1_gate.py.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PROFILES = REPO / "atlas-profiles"
DEPLOY = REPO / "deploy" / "hermes"

ROSTER = yaml.safe_load((PROFILES / "roster.yaml").read_text())["profiles"]
MANAGED = yaml.safe_load((DEPLOY / "managed" / "config.yaml").read_text())
NAMES = sorted(ROSTER)

# PRD §3: the ten specialist profiles.
PRD_PROFILES = {
    "atlas-orchestrator", "market-researcher", "strategy-researcher", "backtest-engineer",
    "risk-analyst", "execution-engineer", "performance-analyst", "data-engineer",
    "jev-analyst", "operations-monitor",
}
# PRD §11: only engineering roles get a shell and file writes.
ENGINEERING = {"backtest-engineer", "execution-engineer", "data-engineer"}
# PRD §9: the four ATLAS bots.
BOTS = {"atlas-orchestrator", "risk-analyst", "operations-monitor", "performance-analyst"}


def load(name: str, file: str) -> dict:
    return yaml.safe_load((PROFILES / name / file).read_text())


def test_roster_matches_prd():
    assert set(ROSTER) == PRD_PROFILES


def test_bots_match_prd():
    assert {n for n, e in ROSTER.items() if e.get("bot")} == BOTS


def test_single_dispatcher_is_the_orchestrator():
    assert [n for n, e in ROSTER.items() if e.get("dispatcher")] == ["atlas-orchestrator"]


@pytest.mark.parametrize("name", NAMES)
def test_distribution_files(name):
    for f in ("distribution.yaml", "SOUL.md", "config.yaml", ".gitignore"):
        assert (PROFILES / name / f).is_file(), f
    manifest = load(name, "distribution.yaml")
    assert manifest["name"] == name
    # Hermes truncates routing descriptions at 280 characters.
    assert 40 <= len(manifest["description"]) <= 280


@pytest.mark.parametrize("name", NAMES)
def test_gitignore_keeps_user_data_out(name):
    ignored = (PROFILES / name / ".gitignore").read_text().split()
    for entry in (".env", "auth.json", "memories/", "sessions/", "state.db*", "logs/"):
        assert entry in ignored


@pytest.mark.parametrize("name", NAMES)
def test_soul_carries_marker_and_hard_rules(name):
    soul = (PROFILES / name / "SOUL.md").read_text()
    assert f"ATLAS profile: {name}" in soul
    assert "holdout" in soul
    assert "never counts as approval" in soul
    assert "kanban_complete(summary, metadata)" in soul


@pytest.mark.parametrize("name", NAMES)
def test_config_safety(name):
    cfg = load(name, "config.yaml")
    assert cfg["terminal"]["backend"] == "docker"
    approvals = cfg["approvals"]
    for mode in ("cron_mode", "unattended_mode", "single_query_mode"):
        assert approvals[mode] == "deny"
    assert approvals["deny"] == MANAGED["approvals"]["deny"]
    assert cfg["terminal"]["docker_mount_cwd_to_workspace"] is (name in ENGINEERING)
    assert "model" not in cfg, "models come from deploy/hermes/models.yaml via the bootstrap"


@pytest.mark.parametrize("name", NAMES)
def test_toolsets_match_roster(name):
    cfg = load(name, "config.yaml")
    entry = ROSTER[name]
    platforms = {"cli", "cron"} | ({entry["bot"]} if entry.get("bot") else set())
    assert set(cfg["platform_toolsets"]) == platforms
    for p in platforms:
        assert cfg["platform_toolsets"][p] == entry["toolsets"]
    assert not set(entry["toolsets"]) & set(MANAGED["agent"]["disabled_toolsets"])


@pytest.mark.parametrize("name", NAMES)
def test_shell_and_files_only_for_engineering(name):
    toolsets = set(ROSTER[name]["toolsets"])
    if name in ENGINEERING:
        assert {"terminal", "file"} <= toolsets
    else:
        assert not toolsets & {"terminal", "file"}


def test_orchestrator_cannot_do_the_work_itself():
    # PRD §3: board, memory and messaging only (skills so it can load its procedures).
    assert set(ROSTER["atlas-orchestrator"]["toolsets"]) <= {"kanban", "memory", "skills"}


def test_operations_monitor_is_read_only():
    assert not set(ROSTER["operations-monitor"]["toolsets"]) & {"terminal", "file", "code_execution", "web"}


@pytest.mark.parametrize("name", NAMES)
def test_kanban_dispatch_only_in_orchestrator_gateway(name):
    kanban = load(name, "config.yaml")["kanban"]
    if name == "atlas-orchestrator":
        assert kanban["dispatch_in_gateway"] is True
        assert kanban["orchestrator_profile"] == name
        assert kanban["default_assignee"] == name
    else:
        assert kanban["dispatch_in_gateway"] is False


@pytest.mark.parametrize("name", NAMES)
def test_bot_profiles_require_an_allowlist(name):
    required = {e["name"] for e in load(name, "distribution.yaml").get("env_requires") or [] if e.get("required")}
    if name in BOTS:
        assert {"TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"} <= required
    else:
        assert not required


def test_nothing_opens_the_gateway_to_everyone():
    for path in list(PROFILES.rglob("*")) + list(DEPLOY.rglob("*")):
        if path.is_file():
            text = path.read_text()
            assert "GATEWAY_ALLOW_ALL_USERS" not in text, path
            assert "allow_all_users: true" not in text, path
    assert MANAGED["gateway"]["allow_all_users"] is False


def test_managed_policy():
    assert MANAGED["terminal"]["backend"] == "docker"
    assert MANAGED["terminal"]["docker_extra_args"] == ["--network", "atlas-agents"]
    assert MANAGED["gateway"]["multiplex_profiles"] is False
    for mode in ("cron_mode", "unattended_mode", "single_query_mode"):
        assert MANAGED["approvals"][mode] == "deny"
    assert {"browser", "cronjob", "computer_use"} <= set(MANAGED["agent"]["disabled_toolsets"])


def test_boards_and_models():
    boards = yaml.safe_load((DEPLOY / "boards.yaml").read_text())["boards"]
    assert set(boards) == {"atlas-research", "atlas-engineering", "atlas-ops"}
    tiers = yaml.safe_load((DEPLOY / "models.yaml").read_text())["tiers"]
    assert {e["tier"] for e in ROSTER.values()} <= set(tiers)
    for t in tiers.values():
        assert t["provider"] and t["model"]
        assert "api_key" not in t
