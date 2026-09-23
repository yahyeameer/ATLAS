"""Static checks and installer steps for phase H3: plugin, ops config, cron jobs (PRD §10, §24, §25)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

from atlas_plugins import jobs

REPO = Path(__file__).resolve().parents[2]
PROFILES = REPO / "atlas-profiles"
ROSTER = yaml.safe_load((PROFILES / "roster.yaml").read_text())["profiles"]
CRON = yaml.safe_load((REPO / "deploy" / "hermes" / "cron.yaml").read_text())["jobs"]
BOARDS = yaml.safe_load((REPO / "deploy" / "hermes" / "boards.yaml").read_text())["boards"]


@pytest.fixture()
def bootstrap():
    spec = importlib.util.spec_from_file_location("atlas_bootstrap", REPO / "deploy" / "hermes" / "bootstrap.py")
    mod = importlib.util.module_from_spec(spec)
    dont_write, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = dont_write
    return mod


# ---------------------------------------------------------------- profiles


@pytest.mark.parametrize("name", sorted(ROSTER))
def test_every_profile_enables_the_atlas_plugin(name):
    cfg = yaml.safe_load((PROFILES / name / "config.yaml").read_text())
    assert cfg["plugins"] == {"enabled": ["atlas"]}


def test_only_operations_monitor_passes_engine_settings_to_scripts():
    for name in ROSTER:
        cfg = yaml.safe_load((PROFILES / name / "config.yaml").read_text())
        passthrough = cfg["terminal"].get("env_passthrough")
        if name == "operations-monitor":
            assert passthrough == ["ATLAS_ENGINE_URL", "ATLAS_TOKEN_OPS_CRON"]
            # The passed-through token must not reach an agent sandbox: this profile has no terminal or code tools.
            assert not {"terminal", "code_execution", "file"} & set(ROSTER[name]["toolsets"])
        else:
            assert not passthrough


def test_operations_monitor_needs_its_home_channel():
    dist = yaml.safe_load((PROFILES / "operations-monitor" / "distribution.yaml").read_text())
    assert "TELEGRAM_HOME_CHANNEL" in {e["name"] for e in dist["env_requires"]}


# ---------------------------------------------------------------- cron.yaml


@pytest.mark.parametrize("name", sorted(CRON))
def test_cron_job_spec(name):
    spec = CRON[name]
    assert name.startswith("atlas-") and spec["profile"] in ROSTER
    assert spec["job"] in jobs.JOBS and spec["deliver"] in ("telegram", "local") and spec["what"]
    assert spec.get("gated_on") in (None, "T2", "T4", "T7")
    if spec["job"] == "card":
        card = spec["card"]
        assert card["board"] in BOARDS and card["assignee"] in ROSTER
        assert card["period"] in ("day", "week", "month") and card["key"] and card["body"].strip()
        assert card.get("tenant") in [None, *BOARDS[card["board"]]["tenants"]]
        for skill in card.get("skills") or []:
            assert skill in (ROSTER[card["assignee"]].get("skills") or []), (name, skill)
    else:
        assert "card" not in spec


def test_prd_schedule_is_covered():
    """PRD §10: 15-minute health, hourly reconciliation, daily and weekly cards, monthly refresh."""
    schedules = {n: s["schedule"] for n, s in CRON.items()}
    assert schedules["atlas-health-check"] == "every 15m"
    assert schedules["atlas-reconciliation-report"] == "0 * * * *"
    periods = sorted(s["card"]["period"] for s in CRON.values() if s["job"] == "card")
    assert periods == ["day", "day", "month", "week", "week", "week"]
    # What runs today needs nothing from later phases.
    assert {n for n, s in CRON.items() if not s.get("gated_on")} == {
        "atlas-health-check", "atlas-alert-relay", "atlas-reconciliation-report"}


# ---------------------------------------------------------------- installer steps


def test_install_plugin_copies_into_every_profile(bootstrap, tmp_path):
    stale = tmp_path / "profiles" / "risk-analyst" / "plugins" / "atlas" / "old.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("x")
    bootstrap.install_plugin(ROSTER, str(tmp_path), False)
    for name in ROSTER:
        d = tmp_path / "profiles" / name / "plugins" / "atlas"
        assert (d / "plugin.yaml").is_file() and (d / "__init__.py").is_file()
        assert (d / "dashboard" / "manifest.json").is_file()
        assert (d / "ATLAS_ROOT").read_text().strip() == str(REPO)
    assert not stale.exists()


def test_install_ops_config(bootstrap, tmp_path):
    path = bootstrap.install_ops_config(str(tmp_path), tmp_path / "experiments.jsonl", False)
    assert path == tmp_path / "atlas" / "ops.yaml"
    cfg = yaml.safe_load(path.read_text())
    assert cfg["research_registry"] == str(tmp_path / "experiments.jsonl")
    assert set(cfg["budgets"]) == {"atlas-research", "atlas-engineering", "atlas-ops", "other"}


def test_cron_script_calls_the_job(bootstrap, monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "main", lambda job, spec=None: calls.append((job, spec)))
    exec(compile(bootstrap.cron_script("atlas-health-check", CRON["atlas-health-check"]), "s", "exec"), {})
    exec(compile(bootstrap.cron_script("atlas-calibration-review", CRON["atlas-calibration-review"]), "s", "exec"), {})
    assert calls[0] == ("health-check", None)
    assert calls[1][0] == "card" and calls[1][1]["name"] == "atlas-calibration-review"
    assert calls[1][1]["key"] == "weekly-calibration"


class FakeHermes:
    dry_run = False

    def __init__(self):
        self.calls = []

    def run(self, *args, check=True):
        self.calls.append(args)


def test_install_cron_creates_updates_and_removes(bootstrap, tmp_path):
    h, home = FakeHermes(), str(tmp_path)
    bootstrap.install_cron(h, CRON, home, enable_gated=False, deliver=None)
    creates = [c for c in h.calls if c[2:4] == ("cron", "create")]
    assert {c[c.index("--name") + 1] for c in creates} == {
        "atlas-health-check", "atlas-alert-relay", "atlas-reconciliation-report"}
    hc = next(c for c in creates if "atlas-health-check" in c)
    assert hc[:2] == ("-p", "operations-monitor") and "--no-agent" in hc and hc[hc.index("--deliver") + 1] == "telegram"
    script = tmp_path / "profiles" / "operations-monitor" / "scripts" / "atlas_health_check.py"
    assert "main('health-check', None)" in script.read_text()

    # Existing jobs (by name) are edited, not duplicated; a gated job that exists is removed.
    jobs_json = tmp_path / "profiles" / "operations-monitor" / "cron" / "jobs.json"
    jobs_json.parent.mkdir(parents=True)
    jobs_json.write_text(json.dumps({"jobs": [{"id": "a1", "name": "atlas-health-check"},
                                              {"id": "b2", "name": "atlas-loss-clusters"},
                                              {"id": "c3", "name": "someone-elses-job"}]}))
    h = FakeHermes()
    bootstrap.install_cron(h, CRON, home, enable_gated=False, deliver="local")
    assert ("-p", "operations-monitor", "cron", "remove", "b2") in h.calls
    edit = next(c for c in h.calls if c[2:4] == ("cron", "edit"))
    assert edit[4] == "a1" and edit[edit.index("--deliver") + 1] == "local"
    assert not any("c3" in c for c in h.calls)

    h = FakeHermes()
    bootstrap.install_cron(h, CRON, home, enable_gated=True, deliver="local")
    names = {c[c.index("--name") + 1] for c in h.calls if c[2:4] == ("cron", "create")}
    assert "atlas-calibration-review" in names and "atlas-daily-report" in names
    assert any(c[:2] == ("-p", "performance-analyst") for c in h.calls)
