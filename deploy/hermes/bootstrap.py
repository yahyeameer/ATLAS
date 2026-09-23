#!/usr/bin/env python3
"""Install the ATLAS profiles, skills, MCP tokens, Kanban boards, plugin and cron jobs into a Hermes home (H1-H3).

Idempotent: re-running reinstalls every profile distribution from
atlas-profiles/ with its pinned skills from atlas-skills/ (memories, sessions
and .env are preserved by Hermes), rewrites each profile's model from its
tier, re-applies the routing descriptions, issues any missing MCP token and
creates any missing board.

    python deploy/hermes/bootstrap.py                     # uses `hermes` on PATH, ~/.hermes
    python deploy/hermes/bootstrap.py --hermes-home /srv/atlas/hermes --repo /srv/atlas/repo \
        --api-tokens /srv/atlas/engine/api-tokens.yaml

Run it with the Python of the venv where ATLAS is installed (`pip install -e '.[mcp]'`):
that interpreter is what the profiles' MCP servers run under.

MCP tokens (H2): each (profile, server) pair in roster.yaml gets its own
token with only the scopes its listed tools need. The token goes into that
profile's .env (0600); only its SHA-256 goes into the API's token file:
--api-tokens for the research API, --engine-tokens for the engine API that
atlas-operations calls (simulated by `atlas-engine-sim` until T4). An existing
token is kept while its scopes still match. It does not start gateways or
write any other secret.

Operations (H3): copies the `atlas` Hermes plugin (atlas_plugins/hermes-plugin)
into every profile, writes <hermes root>/atlas/ops.yaml from
deploy/hermes/ops.yaml, issues ops:read engine tokens for the cron scripts and
the dashboard, and installs the jobs of deploy/hermes/cron.yaml (jobs gated on
a later phase only with --enable-gated).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PROFILES_DIR = REPO / "atlas-profiles"
SKILLS_DIR = REPO / "atlas-skills"
DEPLOY_DIR = REPO / "deploy" / "hermes"
SKILL_CATEGORY = "atlas"  # skills install to <profile>/skills/atlas/<skill>/
PLUGIN_SRC = REPO / "atlas_plugins" / "hermes-plugin"
PLUGIN_NAME = "atlas"

sys.path.insert(0, str(REPO))
from atlas_api import auth  # noqa: E402
from atlas_api.ops import ENGINE_SCOPES  # noqa: E402
from atlas_mcp.scopes import api_of, scopes_for, token_env_var  # noqa: E402

# Engine tokens that are not MCP servers: (profile, .env variable) -> (token name, scopes).
EXTRA_ENGINE_TOKENS = {
    ("operations-monitor", "ATLAS_TOKEN_OPS_CRON"): ("operations-monitor/cron", ["ops:read"]),
    ("atlas-orchestrator", "ATLAS_TOKEN_DASHBOARD"): ("atlas-orchestrator/dashboard", ["ops:read"]),
}


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


class Hermes:
    def __init__(self, binary: str, home: str | None, dry_run: bool):
        self.binary = binary
        self.env = dict(os.environ)
        if home:
            self.env["HERMES_HOME"] = home
        self.dry_run = dry_run

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.binary, *args]
        if self.dry_run:
            print("  $", " ".join(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        proc = subprocess.run(cmd, env=self.env, capture_output=True, text=True)
        if check and proc.returncode != 0:
            sys.stderr.write(proc.stdout + proc.stderr)
            raise SystemExit(f"command failed ({proc.returncode}): {' '.join(cmd)}")
        return proc


def profile_home(home: str | None, name: str) -> Path:
    return (Path(home) if home else Path.home() / ".hermes") / "profiles" / name


def stage_distribution(name: str, skills: list[str], tmp: Path) -> Path:
    """Copy atlas-profiles/<name>/ plus its pinned skills into one installable directory."""
    stage = tmp / name
    shutil.copytree(PROFILES_DIR / name, stage)
    for skill in skills:
        src = SKILLS_DIR / skill
        if not (src / "SKILL.md").exists():
            raise SystemExit(f"roster pins skill {skill!r} for {name}, but {src}/SKILL.md does not exist")
        shutil.copytree(src, stage / "skills" / SKILL_CATEGORY / skill)
    return stage


def prune_skills(target: Path, skills: list[str], dry_run: bool) -> None:
    """Hermes keeps skill dirs a distribution stops shipping; ATLAS pins skills exactly."""
    installed = target / "skills" / SKILL_CATEGORY
    if not installed.is_dir():
        return
    for d in installed.iterdir():
        if d.is_dir() and d.name not in skills:
            print(f"  removing unpinned skill {d.name}")
            if not dry_run:
                shutil.rmtree(d)


def install_profiles(h: Hermes, roster: dict, tiers: dict, home: str | None) -> None:
    for name, entry in roster.items():
        manifest = load_yaml(PROFILES_DIR / name / "distribution.yaml")
        tier = tiers[entry["tier"]]
        skills = entry.get("skills") or []
        print(f"profile {name} ({entry['tier']}: {tier['provider']}/{tier['model']}; skills: {', '.join(skills) or 'none'})")
        with tempfile.TemporaryDirectory(prefix="atlas-dist-") as tmp:
            dist = stage_distribution(name, skills, Path(tmp))
            h.run("profile", "install", str(dist), "--yes", "--force")
        prune_skills(profile_home(home, name), skills, h.dry_run)
        h.run("-p", name, "config", "set", "model.provider", tier["provider"])
        h.run("-p", name, "config", "set", "model.default", tier["model"])
        if tier.get("base_url"):
            h.run("-p", name, "config", "set", "model.base_url", tier["base_url"])
        h.run("profile", "describe", name, "--text", manifest["description"])


def read_env(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Set keys in a profile .env, keeping every other line, mode 0600."""
    lines = path.read_text().splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and k in updates:
            lines[i] = f"{k}={updates[k]}"
            done.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    path.chmod(0o600)


def _ensure_token(env: dict, var: str, token_name: str, scopes: list[str], path: Path, known: dict,
                  dry_run: bool) -> str | None:
    """Keep the token in ``env[var]`` if it is valid with exactly these scopes; else issue a new one."""
    try:
        p = auth.TokenStore.load(path, known).authenticate(env.get(var))
        current = p.name == token_name and sorted(p.scopes) == sorted(scopes)
    except auth.AuthError:
        current = False
    print(f"  {token_name}: {', '.join(scopes)} ({'kept' if current else 'issuing'})")
    if current or dry_run:
        return None
    return auth.issue(path, token_name, scopes, known=known)


def issue_mcp_tokens(roster: dict, home: str | None, tokens_path: Path, api_url: str, python: str,
                     dry_run: bool, engine_tokens: Path | None = None, engine_url: str | None = None) -> None:
    """One scoped token per (profile, MCP server), written to the profile's .env.

    Research-API servers use ``tokens_path``; engine servers (atlas-operations) use
    ``engine_tokens`` with the engine's scope set. The cron scripts and the dashboard
    get their own ops:read engine tokens (EXTRA_ENGINE_TOKENS).
    """
    stores = {"research": (tokens_path, auth.SCOPES), "engine": (engine_tokens, ENGINE_SCOPES)}
    for name, entry in roster.items():
        servers = entry.get("mcp") or {}
        extras = {var: spec for (prof, var), spec in EXTRA_ENGINE_TOKENS.items() if prof == name}
        if not servers and not extras:
            continue
        env_path = profile_home(home, name) / ".env"
        env = read_env(env_path)
        updates = {"ATLAS_MCP_PYTHON": python}
        if any(api_of(s) == "research" for s in servers):
            updates["ATLAS_API_URL"] = api_url
        if extras or any(api_of(s) == "engine" for s in servers):
            if engine_tokens is None or not engine_url:
                raise SystemExit(f"{name} needs engine tokens: pass --engine-tokens and --engine-url")
            updates["ATLAS_ENGINE_URL"] = engine_url
        for server, tools in servers.items():
            path, known = stores[api_of(server)]
            new = _ensure_token(env, token_env_var(server), f"{name}/{server}", scopes_for(server, tools), path, known,
                                dry_run)
            if new:
                updates[token_env_var(server)] = new
        for var, (token_name, scopes) in extras.items():
            new = _ensure_token(env, var, token_name, scopes, engine_tokens, ENGINE_SCOPES, dry_run)
            if new:
                updates[var] = new
        if not dry_run:
            write_env(env_path, updates)


# --------------------------------------------------------------------------- H3: plugin, ops config, cron


def hermes_root(home: str | None) -> Path:
    return Path(home) if home else Path.home() / ".hermes"


def install_plugin(roster: dict, home: str | None, dry_run: bool) -> None:
    """Copy the atlas plugin into every profile; each profile's config.yaml enables it."""
    for name in roster:
        target = profile_home(home, name) / "plugins" / PLUGIN_NAME
        print(f"  {name}: plugins/{PLUGIN_NAME}")
        if dry_run:
            continue
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(PLUGIN_SRC, target, ignore=shutil.ignore_patterns("__pycache__"))
        # Hermes runs the plugin under its own interpreter, which need not have ATLAS installed.
        (target / "ATLAS_ROOT").write_text(f"{REPO}\n")


def install_ops_config(home: str | None, research_registry: Path, dry_run: bool) -> Path:
    """<hermes root>/atlas/ops.yaml from deploy/hermes/ops.yaml. The repo copy is the one to edit."""
    target = hermes_root(home) / "atlas" / "ops.yaml"
    cfg = load_yaml(DEPLOY_DIR / "ops.yaml")
    cfg["research_registry"] = str(research_registry)
    print(f"  {target}")
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Written by deploy/hermes/bootstrap.py from deploy/hermes/ops.yaml; edit that file.\n"
                          + yaml.safe_dump(cfg, sort_keys=False))
    return target


def cron_script(name: str, spec: dict) -> str:
    card = {"name": name, **spec["card"]} if spec.get("card") else None
    return (f"# Written by deploy/hermes/bootstrap.py from deploy/hermes/cron.yaml (job {name}). Do not edit.\n"
            "import sys\n\n"
            "try:\n"
            "    import atlas_plugins  # noqa: F401\n"
            "except ImportError:  # Hermes' own interpreter: use the ATLAS checkout the installer ran from\n"
            f"    sys.path.append({str(REPO)!r})\n\n"
            "from atlas_plugins.jobs import main  # noqa: E402\n\n"
            f"main({spec['job']!r}, {card!r})\n")


def existing_cron_jobs(home: str | None, profile: str) -> dict[str, dict]:
    path = profile_home(home, profile) / "cron" / "jobs.json"
    if not path.exists():
        return {}
    return {j["name"]: j for j in json.loads(path.read_text()).get("jobs", []) if j.get("name")}


def install_cron(h: Hermes, jobs: dict, home: str | None, enable_gated: bool, deliver: str | None) -> None:
    """Create or update each job by name; remove ATLAS jobs whose gate is closed."""
    for name, spec in jobs.items():
        profile = spec["profile"]
        have = existing_cron_jobs(home, profile) if not h.dry_run else {}
        if spec.get("gated_on") and not enable_gated:
            print(f"  {name}: waits on {spec['gated_on']}" + ("; removing" if name in have else ""))
            if name in have:
                h.run("-p", profile, "cron", "remove", have[name]["id"])
            continue
        script = f"atlas_{name.removeprefix('atlas-').replace('-', '_')}.py"
        target = profile_home(home, profile) / "scripts" / script
        if not h.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(cron_script(name, spec))
        target_deliver = deliver or spec["deliver"]
        if name in have:
            print(f"  {name}: updating ({profile}, {spec['schedule']})")
            h.run("-p", profile, "cron", "edit", have[name]["id"], "--schedule", spec["schedule"],
                  "--script", script, "--no-agent", "--deliver", target_deliver)
        else:
            print(f"  {name}: creating ({profile}, {spec['schedule']})")
            h.run("-p", profile, "cron", "create", spec["schedule"], "--no-agent", "--script", script,
                  "--deliver", target_deliver, "--name", name)


def existing_boards(h: Hermes) -> set[str]:
    if h.dry_run:
        return set()
    proc = h.run("kanban", "boards", "list", "--json")
    return {b["slug"] for b in json.loads(proc.stdout)}


def create_boards(h: Hermes, boards: dict, repo_workdir: Path) -> None:
    have = existing_boards(h)
    for slug, spec in boards.items():
        if slug in have:
            print(f"board {slug}: exists")
            continue
        args = ["kanban", "boards", "create", slug, "--name", spec["name"],
                "--description", spec["description"]]
        if spec.get("default_workdir") == "repo":
            args += ["--default-workdir", str(repo_workdir)]
        print(f"board {slug}: creating")
        h.run(*args)


def next_steps(roster: dict, home: str | None, tokens_path: Path, api_url: str) -> None:
    base = Path(home) if home else Path.home() / ".hermes"
    bots = [n for n, e in roster.items() if e.get("bot")]
    print("\nNext steps:")
    print("  1. Install the managed policy: deploy/hermes/managed/config.yaml -> /etc/hermes/config.yaml")
    print("     (root-owned, 0644), or set HERMES_MANAGED_DIR in the service units.")
    print("  2. Create the agent Docker network: sudo deploy/hermes/egress/setup-network.sh")
    print("  3. Give each bot profile its own Telegram bot and the operator's user id:")
    for n in bots:
        print(f"       {base / 'profiles' / n / '.env'}: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS")
    print("  4. Put the model provider key in the service environment file (see deploy/hermes/systemd/).")
    print(f"  5. Start the research API as the engine user, before the gateways:")
    print(f"       atlas-api --tokens {tokens_path} serve --data-root <data> --port {api_url.rsplit(':', 1)[-1]}")
    print("     and, until the engine exists (T4), the simulated engine's operations API:")
    print("       atlas-engine-sim --state <state.json> serve --tokens <engine-tokens.yaml>")
    print("  6. Start the gateways: atlas-orchestrator first (it hosts the Kanban dispatcher).")
    print(f"  7. Dashboard: hermes -p atlas-orchestrator dashboard (localhost only; the ATLAS tab reads {base}/atlas).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hermes", default="hermes", help="hermes executable (default: on PATH)")
    ap.add_argument("--hermes-home", help="HERMES_HOME to install into (default: Hermes' own default)")
    ap.add_argument("--repo", default=str(REPO), help="ATLAS checkout used as the engineering board workdir")
    ap.add_argument("--models", default=str(DEPLOY_DIR / "models.yaml"), help="tier -> model mapping")
    ap.add_argument("--api-tokens", default=str(Path.home() / ".atlas" / "api-tokens.yaml"),
                    help="research API token-hash file, read by `atlas-api serve --tokens`")
    ap.add_argument("--api-url", default="http://127.0.0.1:8741", help="research API URL the MCP servers call")
    ap.add_argument("--atlas-python", default=sys.executable,
                    help="python with ATLAS and the mcp extra installed (default: this interpreter)")
    ap.add_argument("--engine-tokens", default=str(Path.home() / ".atlas" / "engine-tokens.yaml"),
                    help="engine API token-hash file (atlas-engine-sim serve --tokens until T4)")
    ap.add_argument("--engine-url", default="http://127.0.0.1:8742", help="engine API URL for atlas-operations")
    ap.add_argument("--research-registry", default=str(REPO / "research" / "experiments.jsonl"),
                    help="experiment registry shown on the dashboard")
    ap.add_argument("--enable-gated", action="store_true",
                    help="also install cron jobs gated on a later phase (drills only)")
    ap.add_argument("--cron-deliver", help="override every cron job's delivery target (e.g. local for drills)")
    ap.add_argument("--dry-run", action="store_true", help="print the hermes commands without running them")
    args = ap.parse_args()

    if not args.dry_run and shutil.which(args.hermes) is None and not Path(args.hermes).exists():
        raise SystemExit(f"hermes executable not found: {args.hermes}")

    roster = load_yaml(PROFILES_DIR / "roster.yaml")["profiles"]
    tiers = load_yaml(Path(args.models))["tiers"]
    boards = load_yaml(DEPLOY_DIR / "boards.yaml")["boards"]
    missing = {e["tier"] for e in roster.values()} - set(tiers)
    if missing:
        raise SystemExit(f"{args.models} has no model for tier(s): {', '.join(sorted(missing))}")

    h = Hermes(args.hermes, args.hermes_home, args.dry_run)
    install_profiles(h, roster, tiers, args.hermes_home)
    print("MCP tokens")
    issue_mcp_tokens(roster, args.hermes_home, Path(args.api_tokens), args.api_url, args.atlas_python, args.dry_run,
                     Path(args.engine_tokens), args.engine_url)
    create_boards(h, boards, Path(args.repo).resolve())
    print("Plugin")
    install_plugin(roster, args.hermes_home, args.dry_run)
    print("Operations config")
    install_ops_config(args.hermes_home, Path(args.research_registry), args.dry_run)
    print("Cron jobs")
    install_cron(h, load_yaml(DEPLOY_DIR / "cron.yaml")["jobs"], args.hermes_home, args.enable_gated,
                 args.cron_deliver)
    next_steps(roster, args.hermes_home, Path(args.api_tokens), args.api_url)


if __name__ == "__main__":
    main()
