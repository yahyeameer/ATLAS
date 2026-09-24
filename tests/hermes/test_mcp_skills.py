"""Static checks for phase H2: MCP servers and skills wired into the profiles (PRD §3, §6, §7)."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

from atlas_api.auth import SCOPES, TokenStore
from atlas_api.ops import ENGINE_SCOPES
from atlas_mcp.scopes import SERVER_TOOLS, api_of, scopes_for, token_env_var

REPO = Path(__file__).resolve().parents[2]
PROFILES = REPO / "atlas-profiles"
SKILLS = REPO / "atlas-skills"
ROSTER = yaml.safe_load((PROFILES / "roster.yaml").read_text())["profiles"]
NAMES = sorted(ROSTER)
SKILL_NAMES = sorted(d.name for d in SKILLS.iterdir() if d.is_dir())

# PRD §3 MCP column, as far as H2 and H3 build it (atlas-research and atlas-trading come later).
PRD_MCP = {
    "atlas-orchestrator": {"atlas-journal", "atlas-performance"},
    "market-researcher": {"atlas-market"},
    "strategy-researcher": {"atlas-backtest"},
    "backtest-engineer": {"atlas-backtest"},
    "risk-analyst": {"atlas-backtest", "atlas-journal", "atlas-performance"},
    "execution-engineer": {"atlas-operations"},
    "performance-analyst": {"atlas-journal", "atlas-performance"},
    "data-engineer": {"atlas-market"},
    "jev-analyst": {"atlas-backtest"},
    "operations-monitor": {"atlas-operations"},
}
# PRD §7 required sections, and the constraint lines every research skill carries.
SECTIONS = ["Objective", "When to use", "Required inputs", "Procedure", "Tools", "Output format",
            "Safety constraints", "Validation requirements"]
KANBAN_KEYS = ["experiment_id", "strategy_version", "data_window", "trades", "expectancy_r", "pf",
               "max_dd_mc95", "dsr", "artifacts"]


def config(name: str) -> dict:
    return yaml.safe_load((PROFILES / name / "config.yaml").read_text())


def scopes(name: str) -> set[str]:
    return {s for server, tools in (ROSTER[name].get("mcp") or {}).items() for s in scopes_for(server, tools)}


# ---------------------------------------------------------------- MCP wiring

def test_mcp_servers_match_prd():
    assert {n: set(e.get("mcp") or {}) for n, e in ROSTER.items()} == PRD_MCP


@pytest.mark.parametrize("name", NAMES)
def test_roster_tools_exist(name):
    for server, tools in (ROSTER[name].get("mcp") or {}).items():
        assert server in SERVER_TOOLS
        assert tools and len(tools) == len(set(tools))
        assert set(tools) <= set(SERVER_TOOLS[server])


@pytest.mark.parametrize("name", NAMES)
def test_config_mcp_servers_match_roster(name):
    servers = ROSTER[name].get("mcp") or {}
    cfg = config(name).get("mcp_servers") or {}
    assert set(cfg) == set(servers)
    for server, tools in servers.items():
        c = cfg[server]
        assert c["command"] == "${ATLAS_MCP_PYTHON}"
        assert c["args"] == ["-m", "atlas_mcp", server]
        # Exactly two variables reach the server: its API's URL and this server's own token.
        url = "${ATLAS_ENGINE_URL}" if api_of(server) == "engine" else "${ATLAS_API_URL}"
        assert c["env"] == {"ATLAS_API_URL": url, "ATLAS_ENGINE_TOKEN": "${%s}" % token_env_var(server)}
        assert c["tools"]["include"] == tools
        assert c["tools"]["resources"] is False and c["tools"]["prompts"] is False
        assert "url" not in c and "headers" not in c


@pytest.mark.parametrize("name", NAMES)
def test_atlas_tools_are_not_deferred(name):
    """Hermes hides MCP tools behind tool_search by default; ATLAS keeps its few tools direct."""
    assert config(name)["tools"]["tool_search"]["enabled"] == "off"


@pytest.mark.parametrize("name", NAMES)
def test_no_literal_secrets_in_config(name):
    text = (PROFILES / name / "config.yaml").read_text()
    assert "atl_" not in text
    assert not re.search(r"(token|key|secret)\s*:\s*['\"]?[A-Za-z0-9_\-]{20,}", text, re.I)


def test_scopes_follow_the_permission_model():
    # Only the roles that design or build experiments may spend the experiment budget.
    runners = {n for n in NAMES if "backtest:run" in scopes(n)}
    assert runners == {"strategy-researcher", "backtest-engineer", "jev-analyst"}
    # PRD §3: the orchestrator reads the journal and performance, nothing else.
    assert scopes("atlas-orchestrator") == {"journal:read", "performance:read"}
    # PRD §3: risk-analyst reads backtests; it cannot start one.
    assert scopes("risk-analyst") == {"backtest:read", "journal:read", "performance:read"}
    # PRD §3, §23: operations-monitor reads the engine and may only disable; execution-engineer only reads.
    assert scopes("operations-monitor") == {"ops:read", "ops:disable_trading"}
    assert scopes("execution-engineer") == {"ops:read"}
    assert {n for n in NAMES if "ops:disable_trading" in scopes(n)} == {"operations-monitor"}
    for n in NAMES:
        assert scopes(n) <= set(SCOPES) | set(ENGINE_SCOPES)


def test_scope_helper():
    assert scopes_for("atlas-backtest", ["monte_carlo", "list_runs"]) == ["backtest:read"]
    assert scopes_for("atlas-backtest", ["run_backtest", "list_runs"]) == ["backtest:read", "backtest:run"]
    with pytest.raises(ValueError):
        scopes_for("atlas-backtest", ["access_holdout"])
    assert token_env_var("atlas-backtest") == "ATLAS_TOKEN_BACKTEST"


def test_scope_map_matches_the_servers():
    """Each tool's declared scope is the scope of the API route it calls."""
    import asyncio

    from mcp.client import Client

    from atlas_api.http import ROUTES
    from atlas_api.ops import OPS_ROUTES
    from atlas_mcp.servers import SERVERS

    route_scope = {  # mirrors ResearchService and OpsService: each method's p.require(...)
        "market": "market:read", "backtest/run": "backtest:run", "backtest/walk_forward": "backtest:run",
        "backtest/exit_research": "backtest:run",
        "backtest/list_runs": "backtest:read", "backtest/summary": "backtest:read",
        "backtest/monte_carlo": "backtest:read", "journal": "journal:read", "performance": "performance:read",
        "operations": "ops:read", "operations/disable_trading": "ops:disable_trading",
    }

    def scope_of(route):
        return route_scope.get(route) or route_scope[route.split("/")[0]]

    async def tool_routes(server):
        calls = {}
        srv = SERVERS[server](lambda route, **a: calls.setdefault("route", route) and {"ok": True} or {"ok": True})
        out = {}
        async with Client(srv) as c:
            for t in (await c.list_tools()).tools:
                calls.clear()
                args = {k: {"symbol": "EURUSD", "symbols": ["EURUSD"], "timeframe": "H1", "window": "dev",
                            "strategy": "s", "run_id": "r", "reason": "drill reason text"}[k]
                        for k in t.input_schema.get("required", [])}
                await c.call_tool(t.name, args)
                out[t.name] = calls["route"]
        return out

    for server, tools in SERVER_TOOLS.items():
        routes = asyncio.run(tool_routes(server))
        assert set(routes) == set(tools)
        for tool, route in routes.items():
            assert route in (OPS_ROUTES if api_of(server) == "engine" else ROUTES)
            assert tools[tool] == scope_of(route), (server, tool, route)


# ---------------------------------------------------------------- skills

def test_skills():
    # The first 8 (H2), incident-triage (H3) and exit-research (T1).
    assert SKILL_NAMES == sorted([
        "forex-market-analysis", "trend-pullback-research", "session-breakout-research",
        "liquidity-sweep-research", "backtest-analysis", "mfe-mae-analysis", "risk-review", "data-quality",
        "incident-triage", "exit-research"])


def _skill(name):
    text = (SKILLS / name / "SKILL.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, "missing frontmatter"
    return yaml.safe_load(m.group(1)), m.group(2)


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_format(name):
    meta, body = _skill(name)
    assert meta["name"] == name
    assert 20 <= len(meta["description"]) <= 200
    headings = re.findall(r"^## (.+)$", body, re.M)
    assert headings == SECTIONS, headings


# Skills that run or judge experiments carry all of PRD §7's example constraint lines.
EXPERIMENT_SKILLS = {"trend-pullback-research", "session-breakout-research", "liquidity-sweep-research",
                     "backtest-analysis", "mfe-mae-analysis", "risk-review", "exit-research"}


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_safety_lines(name):
    _, body = _skill(name)
    safety = body.split("## Safety constraints", 1)[1].split("## Validation requirements", 1)[0]
    assert "Never request" in safety and "holdout" in safety
    assert "Report every" in safety
    if name in EXPERIMENT_SKILLS:
        assert "R after costs" in safety


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_kanban_metadata_shape(name):
    _, body = _skill(name)
    out = body.split("## Output format", 1)[1].split("## Safety constraints", 1)[0]
    assert "artifacts" in out
    if name in EXPERIMENT_SKILLS:
        assert "experiment_id" in out
    if name in EXPERIMENT_SKILLS - {"mfe-mae-analysis"}:
        for key in KANBAN_KEYS:
            assert key in out, key


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_tools_exist_and_are_granted(name):
    """Every MCP tool a skill names exists, and some profile pinning the skill can call it."""
    _, body = _skill(name)
    all_tools = {t for tools in SERVER_TOOLS.values() for t in tools}
    named = {t for t in all_tools if re.search(rf"`{t}[`(]", body)}
    assert named, "skill names no ATLAS tool"
    holders = [n for n in NAMES if name in (ROSTER[n].get("skills") or [])]
    assert holders, "no profile pins this skill"
    granted = {t for n in holders for tools in (ROSTER[n].get("mcp") or {}).values() for t in tools}
    assert named & granted, (named, granted)


def test_roster_skills_exist():
    for n in NAMES:
        for s in ROSTER[n].get("skills") or []:
            assert (SKILLS / s / "SKILL.md").is_file(), (n, s)
    pinned = {s for e in ROSTER.values() for s in e.get("skills") or []}
    assert pinned == set(SKILL_NAMES)


# ---------------------------------------------------------------- installer

@pytest.fixture()
def bootstrap():
    spec = importlib.util.spec_from_file_location("atlas_bootstrap", REPO / "deploy" / "hermes" / "bootstrap.py")
    mod = importlib.util.module_from_spec(spec)
    dont_write, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # keep deploy/ free of __pycache__
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = dont_write
    return mod


def test_stage_distribution_bundles_pinned_skills(bootstrap, tmp_path):
    stage = bootstrap.stage_distribution("strategy-researcher", ROSTER["strategy-researcher"]["skills"], tmp_path)
    got = sorted(p.parent.name for p in (stage / "skills" / "atlas").glob("*/SKILL.md"))
    assert got == sorted(ROSTER["strategy-researcher"]["skills"])
    assert (stage / "config.yaml").is_file() and (stage / "SOUL.md").is_file()


def test_prune_removes_unpinned_atlas_skills_only(bootstrap, tmp_path):
    (tmp_path / "skills" / "atlas" / "old-skill").mkdir(parents=True)
    (tmp_path / "skills" / "atlas" / "risk-review").mkdir(parents=True)
    (tmp_path / "skills" / "user-made").mkdir(parents=True)
    bootstrap.prune_skills(tmp_path, ["risk-review"], dry_run=False)
    assert not (tmp_path / "skills" / "atlas" / "old-skill").exists()
    assert (tmp_path / "skills" / "atlas" / "risk-review").exists()
    assert (tmp_path / "skills" / "user-made").exists()


def test_issue_mcp_tokens(bootstrap, tmp_path):
    home, tokens = tmp_path / "hermes", tmp_path / "engine" / "api-tokens.yaml"
    engine_tokens, engine_url = tmp_path / "engine" / "engine-tokens.yaml", "http://127.0.0.1:8742"
    env_path = home / "profiles" / "risk-analyst" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("TELEGRAM_BOT_TOKEN=keep-me\n")

    def issue(roster=ROSTER):
        bootstrap.issue_mcp_tokens(roster, str(home), tokens, "http://127.0.0.1:8741", "/venv/bin/python", False,
                                   engine_tokens, engine_url)

    issue()
    env = bootstrap.read_env(env_path)
    assert env["TELEGRAM_BOT_TOKEN"] == "keep-me"
    assert env["ATLAS_API_URL"] == "http://127.0.0.1:8741" and env["ATLAS_MCP_PYTHON"] == "/venv/bin/python"
    assert "ATLAS_ENGINE_URL" not in env
    assert oct(env_path.stat().st_mode & 0o777) == "0o600"
    store = TokenStore.load(tokens)
    p = store.authenticate(env["ATLAS_TOKEN_BACKTEST"])
    assert p.name == "risk-analyst/atlas-backtest" and p.scopes == {"backtest:read"}
    assert env["ATLAS_TOKEN_BACKTEST"] not in tokens.read_text()
    # Every (profile, server) pair got its own token in its API's file.
    names = {t["name"] for t in yaml.safe_load(tokens.read_text())["tokens"]}
    pairs = {(n, s) for n in NAMES for s in ROSTER[n].get("mcp") or {}}
    assert names == {f"{n}/{s}" for n, s in pairs if api_of(s) == "research"}
    engine_names = {t["name"] for t in yaml.safe_load(engine_tokens.read_text())["tokens"]}
    assert engine_names == {f"{n}/{s}" for n, s in pairs if api_of(s) == "engine"} | {
        "operations-monitor/cron", "atlas-orchestrator/dashboard"}

    # operations-monitor: its MCP token may disable; its cron token only reads. Neither works on the research API.
    om = bootstrap.read_env(home / "profiles" / "operations-monitor" / ".env")
    assert om["ATLAS_ENGINE_URL"] == engine_url and "ATLAS_API_URL" not in om
    estore = TokenStore.load(engine_tokens, ENGINE_SCOPES)
    assert estore.authenticate(om["ATLAS_TOKEN_OPERATIONS"]).scopes == {"ops:read", "ops:disable_trading"}
    assert estore.authenticate(om["ATLAS_TOKEN_OPS_CRON"]).scopes == {"ops:read"}
    for tok in (om["ATLAS_TOKEN_OPERATIONS"], om["ATLAS_TOKEN_OPS_CRON"]):
        with pytest.raises(PermissionError):
            store.authenticate(tok)
    with pytest.raises(PermissionError):
        estore.authenticate(env["ATLAS_TOKEN_BACKTEST"])
    orch = bootstrap.read_env(home / "profiles" / "atlas-orchestrator" / ".env")
    assert estore.authenticate(orch["ATLAS_TOKEN_DASHBOARD"]).scopes == {"ops:read"}
    ee = bootstrap.read_env(home / "profiles" / "execution-engineer" / ".env")
    assert estore.authenticate(ee["ATLAS_TOKEN_OPERATIONS"]).scopes == {"ops:read"}

    # Re-running keeps valid tokens.
    before = env_path.read_text(), (home / "profiles" / "operations-monitor" / ".env").read_text()
    issue()
    assert (env_path.read_text(), (home / "profiles" / "operations-monitor" / ".env").read_text()) == before
    # A roster change in scopes reissues that token only.
    roster = {**ROSTER, "risk-analyst": {**ROSTER["risk-analyst"], "mcp": {
        **ROSTER["risk-analyst"]["mcp"], "atlas-backtest": ["run_backtest", "list_runs"]}}}
    issue(roster)
    env2 = bootstrap.read_env(env_path)
    assert env2["ATLAS_TOKEN_BACKTEST"] != env["ATLAS_TOKEN_BACKTEST"]
    assert env2["ATLAS_TOKEN_JOURNAL"] == env["ATLAS_TOKEN_JOURNAL"]
    with pytest.raises(PermissionError):
        TokenStore.load(tokens).authenticate(env["ATLAS_TOKEN_BACKTEST"])


def test_issue_mcp_tokens_needs_engine_settings(bootstrap, tmp_path):
    with pytest.raises(SystemExit, match="engine"):
        bootstrap.issue_mcp_tokens(ROSTER, str(tmp_path), tmp_path / "t.yaml", "http://x", "/py", False)
