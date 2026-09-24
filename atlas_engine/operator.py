"""Signed operator commands (PRD §11 layer 4, §23): the only way to re-enable, kill or flatten.

Enabling trading, clearing a kill, re-enabling after the drawdown stop and
flattening need the human operator, authenticated outside Hermes. The
operator signs a command on the engine host with a key only the operator and
the engine's OS user can read; the engine checks the signature, the command's
age and that its nonce was never used, then applies it. Commands arrive as
files in the engine's operator inbox directory, which no agent can reach: the
agents run on another host and the engine API has no route that accepts one.

The signature is HMAC-SHA256 over the canonical JSON of the command. A key is
32 random bytes, hex-encoded, in a file readable only by its owner. Telegram
and every other agent channel carry alerts only and can't produce a command
(decision 7 in docs/open-questions-decisions.md).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

ACTIONS = {
    "enable_trading": "Re-enable new trades (refused while the engine is in HALT or KILL)",
    "kill": "Manual kill: flatten every ATLAS position and disable trading",
    "flatten": "Close every ATLAS position; trading stays as it is",
    "clear_kill": "Clear a manual kill (trading stays disabled until enable_trading)",
    "reenable_drawdown": "Clear the internal drawdown stop (the risk engine re-latches if equity is still past it)",
}
MAX_AGE_S = 120
MIN_REASON = 10


class OperatorAuthError(PermissionError):
    pass


def keygen(path: str | Path) -> None:
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} exists; remove it first to rotate the key")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_hex(32) + "\n")


def load_key(path: str | Path, check_mode: bool = True) -> bytes:
    path = Path(path)
    if check_mode and os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise OperatorAuthError(f"{path} must be readable by its owner only (chmod 600)")
    key = bytes.fromhex(path.read_text().strip())
    if len(key) < 32:
        raise OperatorAuthError("operator key must be at least 32 bytes")
    return key


def _payload(cmd: dict) -> bytes:
    return json.dumps({k: v for k, v in cmd.items() if k != "sig"}, sort_keys=True, separators=(",", ":")).encode()


def sign(key: bytes, action: str, operator: str, reason: str, now: dt.datetime | None = None) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}; known: {', '.join(ACTIONS)}")
    if len(reason.strip()) < MIN_REASON:
        raise ValueError(f"reason must be at least {MIN_REASON} characters")
    cmd = {"action": action, "operator": operator, "reason": reason.strip(),
           "at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(), "nonce": secrets.token_hex(16)}
    cmd["sig"] = hmac.new(key, _payload(cmd), hashlib.sha256).hexdigest()
    return cmd


def verify(key: bytes, cmd: dict, now: dt.datetime, seen_nonces: set[str]) -> dict:
    """Return the command if it is authentic, fresh and new; raise OperatorAuthError otherwise."""
    if not isinstance(cmd, dict) or set(cmd) != {"action", "operator", "reason", "at", "nonce", "sig"}:
        raise OperatorAuthError("malformed operator command")
    good = hmac.new(key, _payload(cmd), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, str(cmd["sig"])):
        raise OperatorAuthError("bad signature")
    if cmd["action"] not in ACTIONS:
        raise OperatorAuthError(f"unknown action {cmd['action']!r}")
    try:
        at = dt.datetime.fromisoformat(cmd["at"])
    except (TypeError, ValueError):
        raise OperatorAuthError("bad timestamp") from None
    if at.tzinfo is None or abs((now - at).total_seconds()) > MAX_AGE_S:
        raise OperatorAuthError(f"command is older than {MAX_AGE_S} s or from the future")
    if cmd["nonce"] in seen_nonces:
        raise OperatorAuthError("command was already used (replay)")
    return cmd
