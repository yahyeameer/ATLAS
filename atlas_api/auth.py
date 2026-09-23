"""Scoped API tokens (PRD §6: "authorization is enforced in the engine API by token scope").

Tokens are random strings handed to one MCP server of one profile. Only their
SHA-256 is stored, in a YAML file owned by the engine's OS user:

    tokens:
      - name: risk-analyst/atlas-backtest
        sha256: 5f1c...
        scopes: [backtest:read]
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from pathlib import Path

import yaml

# Every scope the research API knows. Write scopes that could touch live
# trading or risk (trading:*, risk:*, holdout:*) do not exist here at all.
SCOPES = {
    "market:read": "Bars, spreads and compact market state (dev and validation data only)",
    "backtest:run": "Run backtests and walk-forward experiments (counted against the experiment budget)",
    "backtest:read": "Read experiment summaries and run Monte Carlo on recorded runs",
    "journal:read": "Query recorded trades, MFE/MAE and loss clusters",
    "performance:read": "Performance summaries over recorded trades",
}


class AuthError(PermissionError):
    """Unknown or missing token (HTTP 401)."""


class ScopeError(PermissionError):
    """Valid token without the scope the call needs (HTTP 403)."""


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ScopeError(f"token '{self.name}' lacks scope '{scope}'")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    """Token hashes of one API. ``known`` is that API's scope set: the research API's
    ``SCOPES`` by default, or the engine's ``atlas_api.ops.ENGINE_SCOPES``."""

    def __init__(self, entries: list[dict], known: dict[str, str] = SCOPES):
        self._by_hash: dict[str, Principal] = {}
        for e in entries:
            unknown = set(e["scopes"]) - set(known)
            if unknown:
                raise ValueError(f"token '{e['name']}' has unknown scope(s): {', '.join(sorted(unknown))}")
            self._by_hash[e["sha256"]] = Principal(e["name"], frozenset(e["scopes"]))

    @classmethod
    def load(cls, path: Path, known: dict[str, str] = SCOPES) -> "TokenStore":
        data = yaml.safe_load(Path(path).read_text()) if Path(path).exists() else None
        return cls((data or {}).get("tokens") or [], known)

    def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise AuthError("missing bearer token")
        digest = hash_token(token)
        for stored, principal in self._by_hash.items():
            if hmac.compare_digest(stored, digest):
                return principal
        raise AuthError("unknown token")


def issue(path: Path, name: str, scopes: list[str], known: dict[str, str] = SCOPES) -> str:
    """Create a token, store its hash (replacing any token with the same name) and return it once."""
    unknown = set(scopes) - set(known)
    if unknown:
        raise ValueError(f"unknown scope(s): {', '.join(sorted(unknown))}")
    path = Path(path)
    data = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    tokens = [t for t in data.get("tokens") or [] if t["name"] != name]
    token = "atl_" + secrets.token_urlsafe(32)
    tokens.append({"name": name, "sha256": hash_token(token), "scopes": sorted(scopes)})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"tokens": tokens}, sort_keys=False))
    path.chmod(0o600)
    return token
