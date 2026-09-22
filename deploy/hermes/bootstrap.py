#!/usr/bin/env python3
"""Install the ATLAS profiles, skills, MCP tokens and Kanban boards into a Hermes home (H1, H2).

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
research-API token with only the scopes its listed tools need. The token goes
into that profile's .env (0600); only its SHA-256 goes into --api-tokens,
which `atlas-api serve --tokens` reads. An existing token is kept while its
scopes still match. It does not start gateways or write any other secret.
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

sys.path.insert(0, str(REPO))
from atlas_api import auth  # noqa: E402
from atlas_mcp.scopes import scopes_for, token_env_var  # noqa: E402


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


def issue_mcp_tokens(roster: dict, home: str | None, tokens_path: Path, api_url: str, python: str,
                     dry_run: bool) -> None:
    """One scoped research-API token per (profile, MCP server), written to the profile's .env."""
    for name, entry in roster.items():
        servers = entry.get("mcp") or {}
        if not servers:
            continue
        env_path = profile_home(home, name) / ".env"
        env = read_env(env_path)
        store = auth.TokenStore.load(tokens_path)
        updates = {"ATLAS_API_URL": api_url, "ATLAS_MCP_PYTHON": python}
        for server, tools in servers.items():
            token_name, scopes = f"{name}/{server}", scopes_for(server, tools)
            var = token_env_var(server)
            try:
                p = store.authenticate(env.get(var))
                current = p.name == token_name and sorted(p.scopes) == scopes
            except auth.AuthError:
                current = False
            print(f"  {token_name}: {', '.join(scopes)} ({'kept' if current else 'issuing'})")
            if not current and not dry_run:
                updates[var] = auth.issue(tokens_path, token_name, scopes)
        if not dry_run:
            write_env(env_path, updates)


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
    print("  6. Start the gateways: atlas-orchestrator first (it hosts the Kanban dispatcher).")


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
    issue_mcp_tokens(roster, args.hermes_home, Path(args.api_tokens), args.api_url, args.atlas_python, args.dry_run)
    create_boards(h, boards, Path(args.repo).resolve())
    next_steps(roster, args.hermes_home, Path(args.api_tokens), args.api_url)


if __name__ == "__main__":
    main()
