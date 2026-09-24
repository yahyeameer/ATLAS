"""Engine-side adapter for Jev, the calibrated setup evaluator (PRD §17).

``JevAdapter.evaluate_setup`` is the one call the engine and the research
harness make. It enforces the §17 contract whatever sits behind it:

- **Leakage guard.** The request holds only the whitelisted, normalised state
  from ``atlas_engine.decisions.state`` plus the planned target and cost in R.
  Unknown keys, and strings that look like dates or symbols, raise
  ``LeakageError`` before anything is sent.
- **Pinned wording and model.** Questions are fixed here and versioned
  (``QUESTIONS_VERSION``); ``jev-latest`` or an empty model version is refused
  in live mode. Changing either is a new strategy version.
- **Skip, never guess.** A timeout (500 ms), a transport error, a schema
  failure or an out-of-range value returns ``Skip`` with a reason.
- **Determinism.** Responses are cached by the hash of the request, so the
  same state always gets the same answer within a model version.
- **No authority.** The response is a probability and a regime label. Jev
  never sees account balance and nothing here touches risk, size or orders.

The transport is injected. ATLAS ships ``ReplayTransport`` (answers from a
recorded file, for research replays and tests) and no network client: the
vendor client, its endpoint and its credentials belong to the engine host's
deployment, not this repository.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from atlas_engine.decisions.state import CATEGORICAL, NUMERIC, REGIMES

QUESTIONS_VERSION = "atlas-jev-q1"
QUESTIONS = {
    "p_target_first": (
        "Given this setup state, will price reach the planned target before the planned stop? Answer yes or no."
    ),
    "regime": "Which regime best describes the market in this state? Choose one of: " + ", ".join(REGIMES) + ".",
}

REQUEST_KEYS = frozenset((*NUMERIC, *CATEGORICAL, "setup", "target_r", "cost_r", "recent_signal_r20"))
_DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}|\d{10,}")
_SYMBOL_LIKE = re.compile(r"\b(?:[A-Z]{6}|XAU|XAG|EUR|USD|GBP|JPY|CHF|AUD|NZD|CAD)\b", re.IGNORECASE)
_REASON_CODE = re.compile(r"^[a-z0-9_]{1,40}$")


class LeakageError(ValueError):
    """The request would send something §17 forbids (dates, prices, symbols, unknown fields)."""


class Transport(Protocol):
    def __call__(self, request: dict) -> dict: ...


@dataclass(frozen=True)
class JevResult:
    setup_id: str
    p_target_first: float
    regime: str
    reason_codes: tuple[str, ...]
    model_version: str
    latency_ms: float
    cached: bool = False


@dataclass(frozen=True)
class Skip:
    setup_id: str
    reason: str
    latency_ms: float | None = None


def check_state(state: dict) -> dict:
    """Validate and normalise the model-facing state, raising ``LeakageError`` on anything outside §17."""
    extra = set(state) - REQUEST_KEYS
    if extra:
        raise LeakageError(f"fields not allowed in a Jev request: {sorted(extra)}")
    out = {}
    for k in sorted(REQUEST_KEYS):
        v = state.get(k)
        if k in CATEGORICAL:
            if v not in CATEGORICAL[k]:
                raise LeakageError(f"{k}={v!r} is not one of the fixed labels")
        elif k == "setup":
            if not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", v) or _SYMBOL_LIKE.search(v.replace("_", " ")):
                raise LeakageError(f"setup name {v!r} is not a plain setup identifier")
        else:
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                raise LeakageError(f"{k} must be a number in ATR/R units, got {type(v).__name__}")
            if v is not None and not math.isfinite(v):
                v = None
            v = None if v is None else round(float(v), 4)
        if isinstance(v, str) and _DATE_LIKE.search(v):
            raise LeakageError(f"{k} looks like a date or time")
        out[k] = v
    return out


def request_hash(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:32]


class ReplayTransport:
    """Answers from recorded responses keyed by request hash; unknown requests raise ``KeyError``.

    The file is JSON lines of ``{"request_hash": ..., "response": {...}}``.
    """

    def __init__(self, responses: dict[str, dict] | None = None, path: Path | None = None):
        self.responses = dict(responses or {})
        if path is not None:
            for line in Path(path).read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.responses[rec["request_hash"]] = rec["response"]

    def __call__(self, request: dict) -> dict:
        return self.responses[request_hash(_cache_key(request))]


def _cache_key(request: dict) -> dict:
    return {k: v for k, v in request.items() if k != "setup_id"}


@dataclass
class JevAdapter:
    transport: Transport | Callable[[dict], dict]
    model_version: str
    live: bool = False
    timeout_ms: float = 500.0
    cache: dict = field(default_factory=dict)
    latencies_ms: list = field(default_factory=list)

    def __post_init__(self):
        if self.live and (not self.model_version or self.model_version.endswith("latest")):
            raise ValueError("live mode needs a pinned Jev model version, never jev-latest (PRD §17)")
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev")

    def build_request(self, setup_id: str, state: dict, target_r: float, cost_r: float) -> dict:
        return {
            "setup_id": setup_id,
            "model_version": self.model_version,
            "questions_version": QUESTIONS_VERSION,
            "questions": QUESTIONS,
            "state": check_state({**state, "target_r": target_r, "cost_r": cost_r}),
        }

    def evaluate_setup(self, setup_id: str, state: dict, target_r: float, cost_r: float) -> JevResult | Skip:
        request = self.build_request(setup_id, state, target_r, cost_r)
        key = request_hash(_cache_key(request))
        if key in self.cache:
            hit = self.cache[key]
            if isinstance(hit, JevResult):
                return JevResult(setup_id, hit.p_target_first, hit.regime, hit.reason_codes, hit.model_version, 0.0, True)
        t0 = time.perf_counter()
        try:
            raw = self._pool.submit(self.transport, request).result(timeout=self.timeout_ms / 1000)
        except FutureTimeout:
            ms = (time.perf_counter() - t0) * 1000
            self.latencies_ms.append(ms)
            return Skip(setup_id, "timeout", ms)
        except Exception as e:  # noqa: BLE001 - any transport failure is a skip, never a guess
            ms = (time.perf_counter() - t0) * 1000
            self.latencies_ms.append(ms)
            return Skip(setup_id, f"transport_error:{type(e).__name__}", ms)
        ms = (time.perf_counter() - t0) * 1000
        self.latencies_ms.append(ms)
        if ms > self.timeout_ms:
            return Skip(setup_id, "timeout", ms)
        result = self._validate(setup_id, raw, ms)
        if isinstance(result, JevResult):
            self.cache[key] = result
        return result

    def _validate(self, setup_id: str, raw, ms: float) -> JevResult | Skip:
        if not isinstance(raw, dict):
            return Skip(setup_id, "schema:not_an_object", ms)
        if raw.get("setup_id") != setup_id:
            return Skip(setup_id, "schema:setup_id_mismatch", ms)
        if raw.get("model_version") != self.model_version:
            return Skip(setup_id, "schema:model_version_mismatch", ms)
        p = raw.get("p_target_first")
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p):
            return Skip(setup_id, "schema:p_target_first", ms)
        if not 0.0 <= p <= 1.0:
            return Skip(setup_id, "out_of_range:p_target_first", ms)
        regime = raw.get("regime")
        if regime not in REGIMES:
            return Skip(setup_id, "schema:regime", ms)
        codes = raw.get("reason_codes", [])
        if not isinstance(codes, list) or not all(isinstance(c, str) and _REASON_CODE.match(c) for c in codes):
            return Skip(setup_id, "schema:reason_codes", ms)
        return JevResult(setup_id, float(p), regime, tuple(codes), self.model_version, ms)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
