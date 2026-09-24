"""Research API methods behind the ATLAS MCP servers (PRD §6).

Every method takes the calling ``Principal`` first, checks its scope, and
refuses any date window that reaches the locked holdout (PRD §13, §22). There
is no parameter, window name or method that returns holdout data; the
human-run holdout step (T6) lives outside this service.

"Journal" here means recorded research trades (backtest and walk-forward
runs). Paper and live fills join it when the engine journal exists (T4, T7).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from atlas_engine.features.frame import build_features
from atlas_engine.market_data import bars as mbars
from atlas_engine.market_data.symbols import spec
from atlas_engine.setups import SETUPS, EdgeFilters
from atlas_research import metrics
from atlas_research.backtest import ExitPolicy
from atlas_research.registry import BudgetExceeded, Registry
from atlas_research.t0 import Runner, expand_grid, in_window, prepare_market, run_t0

from .auth import Principal

LoadM1 = Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame]

MAX_BARS = 500
MAX_TRADES = 200
MAX_MC_SIMS = 20_000
TIMEFRAMES = {"M15": "15min", "H1": "1h"}


class HoldoutRefused(PermissionError):
    """The request reaches into the locked holdout (HTTP 403, code holdout_refused)."""


class BadRequest(ValueError):
    """Invalid arguments (HTTP 400)."""


class NotFound(LookupError):
    """Unknown run id (HTTP 404)."""


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class ResearchService:
    def __init__(self, cfg: dict, load_m1: LoadM1, registry: Registry, runs_dir: Path,
                 now: Callable[[], dt.datetime] | None = None):
        self.cfg = cfg
        self._load = load_m1
        self.registry = registry
        self.runs_dir = Path(runs_dir)
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        seg = cfg["segments"]
        self.holdout_start = _utc(seg["holdout_start"])
        self.windows = {
            "dev": (_utc(seg["dev"][0]), _utc(seg["dev"][1])),
            "validation": (_utc(seg["validation"][0]), _utc(seg["validation"][1])),
        }

    # ------------------------------------------------------------------ guards

    def window(self, w) -> tuple[pd.Timestamp, pd.Timestamp]:
        """Resolve "dev", "validation" or {"start", "end"} (end exclusive); refuse the holdout."""
        if isinstance(w, str):
            if w not in self.windows:
                if "holdout" in w.lower():
                    raise HoldoutRefused("the holdout is locked; no agent token can read it")
                raise BadRequest(f"unknown window {w!r}; use 'dev', 'validation' or {{start, end}}")
            return self.windows[w]
        if not isinstance(w, dict) or "start" not in w or "end" not in w:
            raise BadRequest("window must be 'dev', 'validation' or {start, end}")
        try:
            start, end = _utc(w["start"]), _utc(w["end"])
        except (ValueError, TypeError) as e:
            raise BadRequest(f"bad window dates: {e}") from None
        if end <= start:
            raise BadRequest("window end must be after start")
        if end > self.holdout_start:
            raise HoldoutRefused(
                f"window ends {end:%Y-%m-%d}; data from {self.holdout_start:%Y-%m-%d} on is the locked holdout")
        return start, end

    def _symbols(self, symbols) -> list[str]:
        if not symbols or not isinstance(symbols, list):
            raise BadRequest("symbols must be a non-empty list")
        out = []
        for s in symbols:
            try:
                out.append(spec(str(s)).name)
            except KeyError as e:
                raise BadRequest(str(e)) from None
        return out

    def _m1(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        if end > self.holdout_start:  # defence in depth; window() already refuses
            raise HoldoutRefused("refusing to load holdout rows")
        return self._load(symbol, start, end)

    # ------------------------------------------------------------------ market:read

    def market_state(self, p: Principal, symbols: list[str]) -> dict:
        """Compact state per symbol at the last bar before the holdout."""
        p.require("market:read")
        end = self.windows["validation"][1]
        out = {}
        for sym in self._symbols(symbols):
            m1 = self._m1(sym, end - pd.Timedelta(days=90), end)
            if m1.empty:
                out[sym] = {"error": "no data"}
                continue
            f = build_features(m1).dropna(subset=["atr"])
            last = f.iloc[-1]
            pip = spec(sym).pip
            out[sym] = {
                "as_of": str(last["close_time"]),
                "mid": round(float(last["close"]), 6),
                "atr_m15_pips": round(float(last["atr"]) / pip, 2),
                "atr_percentile_60d": _num(last.get("atr_pct")),
                "h1_trend": int(last["h1_trend"]),
                "h1_adx": _num(last.get("h1_adx")),
                "spread_pips_median_5d": round(float(f["spread"].tail(96 * 5).median()) / pip, 2),
                "prior_day_high": _num(last.get("pdh"), 6),
                "prior_day_low": _num(last.get("pdl"), 6),
            }
        return {"symbols": out, "note": "research data ends where the holdout begins"}

    def bars(self, p: Principal, symbol: str, timeframe: str, window, limit: int = 200) -> dict:
        p.require("market:read")
        if timeframe not in TIMEFRAMES:
            raise BadRequest(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
        limit = _clamp(limit, 1, MAX_BARS)
        (sym,) = self._symbols([symbol])
        start, end = self.window(window)
        b = mbars.with_mid(mbars.resample(self._m1(sym, start, end), TIMEFRAMES[timeframe])).tail(limit)
        pip = spec(sym).pip
        rows = [[str(t), round(r.open, 6), round(r.high, 6), round(r.low, 6), round(r.close, 6), round(r.spread / pip, 2)]
                for t, r in b.iterrows()]
        return {"symbol": sym, "timeframe": timeframe, "columns": ["time", "open", "high", "low", "close", "spread_pips"],
                "rows": rows, "truncated_to_last": limit}

    def spread_stats(self, p: Principal, symbol: str, window) -> dict:
        p.require("market:read")
        (sym,) = self._symbols([symbol])
        start, end = self.window(window)
        m1 = self._m1(sym, start, end)
        if m1.empty:
            return {"symbol": sym, "sessions": {}}
        spread = (m1["ask_c"] - m1["bid_c"]) / spec(sym).pip
        label = metrics.session_label(m1.index)
        g = spread.groupby(label)
        stats = pd.DataFrame({"median": g.median(), "p95": g.quantile(0.95), "minutes": g.size()}).round(3)
        return {"symbol": sym, "window": [str(start), str(end)], "unit": "pips", "sessions": stats.to_dict("index")}

    # ------------------------------------------------------------------ backtest:run

    def run_backtest(self, p: Principal, strategy: str, window="validation", params: dict | None = None,
                     symbols: list[str] | None = None, spread_mult: float | None = None) -> dict:
        """One parameter set on one window. Recorded in the registry as a trial."""
        p.require("backtest:run")
        scfg = self._strategy(strategy)
        start, end = self.window(window)
        allowed = scfg["symbols"]
        syms = self._symbols(symbols) if symbols else list(allowed)
        if set(syms) - set(allowed):
            raise BadRequest(f"{strategy} is validated for {', '.join(allowed)} only (PRD §16)")
        setup = SETUPS[scfg.get("setup", strategy)]
        params = {**setup.defaults, **(params or {})}
        unknown = set(params) - set(setup.defaults)
        if unknown:
            raise BadRequest(f"unknown parameter(s) for {strategy}: {', '.join(sorted(unknown))}")
        mult = float(spread_mult or self.cfg["costs"]["spread_mult"])
        if not 1.0 <= mult <= 3.0:
            raise BadRequest("spread_mult must be between 1.0 and 3.0")
        self._budget(strategy)

        markets = [prepare_market(s, self._m1(s, start, end), self.cfg, scfg.get("bar", "15min")) for s in syms]
        runner = Runner(setup, markets, self._exits(), EdgeFilters(**self.cfg["filters"]))
        trades = in_window(runner.trades(params, mult), (start, end))
        s = metrics.summary(trades)
        run_id = self._run_id(strategy, params, start, end, mult)
        created = self._now().isoformat()
        summary = {
            "run_id": run_id, "kind": "backtest", "strategy": strategy, "strategy_version": setup.version,
            "created_at": created, "requested_by": p.name, "symbols": syms, "params": params,
            "window": [str(start), str(end)], "spread_mult": mult, "summary": s,
            "kanban_metadata": {
                "experiment_id": run_id, "strategy_version": setup.version,
                "data_window": window if isinstance(window, str) else f"{start:%Y-%m-%d}..{end:%Y-%m-%d}",
                "trades": s["trades"], "expectancy_r": s["expectancy_r"], "pf": s["profit_factor"],
                "max_dd_mc95": None, "dsr": None, "artifacts": [],
            },
        }
        self._save_run(run_id, summary, trades)
        self.registry.append({
            "experiment_id": run_id, "kind": "backtest", "strategy": strategy, "setup": scfg.get("setup", strategy),
            "strategy_version": setup.version, "created_at": created, "requested_by": p.name, "hypothesis": scfg.get("hypothesis", ""),
            "symbols": syms, "data_window": summary["kanban_metadata"]["data_window"], "grid": [params],
            "trial_sharpes": [s["sharpe_per_trade"]], "final_params": params, "passed": None,
            "kanban_metadata": summary["kanban_metadata"],
        })
        return _jsonable(summary)

    def run_walk_forward(self, p: Principal, strategy: str) -> dict:
        """The full T0 pipeline for a configured strategy: dev walk-forward, validation once, every gate."""
        p.require("backtest:run")
        self._strategy(strategy)
        self._budget(strategy)
        res = run_t0(strategy, self.cfg, self._m1, self.registry, self.runs_dir, now=self._now())
        keep = ("experiment_id", "strategy", "strategy_version", "passed", "final_params", "dev_oos", "validation",
                "walk_forward", "dsr", "kanban_metadata")
        out = {k: res[k] for k in keep if k in res}
        out["run_id"] = res["experiment_id"]
        out["failed_gates"] = [f"{g['gate']} ({g['scope']})" for g in res["gates"] if not g["passed"]]
        out["gates"] = [{k: g[k] for k in ("gate", "scope", "value", "rule", "passed")} for g in res["gates"]]
        return _jsonable(out)

    # ------------------------------------------------------------------ backtest:read

    def list_runs(self, p: Principal, strategy: str | None = None, limit: int = 20) -> dict:
        p.require("backtest:read")
        entries = [e for e in self.registry.entries(strategy) if not self._is_holdout_record(e)]
        entries = entries[-_clamp(limit, 1, 100):]
        return {"runs": [{
            "run_id": e["experiment_id"], "kind": e.get("kind", "walk_forward"), "strategy": e["strategy"],
            "created_at": e["created_at"], "passed": e.get("passed"), "data_window": e.get("data_window"),
            "trades": (e.get("kanban_metadata") or {}).get("trades"),
            "expectancy_r": (e.get("kanban_metadata") or {}).get("expectancy_r"),
        } for e in reversed(entries)]}

    def run_summary(self, p: Principal, run_id: str) -> dict:
        p.require("backtest:read")
        summary = self._load_summary(run_id)
        for k in ("folds", "neighbours"):
            summary.pop(k, None)
        if isinstance(summary.get("walk_forward"), dict):
            summary["walk_forward"] = {"efficiency": summary["walk_forward"].get("efficiency")}
        return _jsonable(summary)

    def monte_carlo(self, p: Principal, run_id: str, sims: int = 10_000, skip_frac: float = 0.0) -> dict:
        p.require("backtest:read")
        if not 0.0 <= skip_frac < 0.5:
            raise BadRequest("skip_frac must be in [0, 0.5)")
        t = self._trades(run_id)
        acct = self.cfg["account"]
        r = t["r"].to_numpy(float)
        sims = _clamp(sims, 100, MAX_MC_SIMS)
        mc = metrics.mc_drawdown(r, acct["risk_pct"], sims, skip_frac=skip_frac)
        breach = metrics.daily_breach_probability(t, acct["risk_pct"], acct["firm_daily_loss_pct"],
                                                  acct["eval_days"], sims)
        return _jsonable({"run_id": run_id, "trades": len(r), "sims": sims, "skip_frac": skip_frac,
                          "risk_pct_per_trade": acct["risk_pct"], **mc, "daily_breach_prob": breach,
                          "firm_max_drawdown_pct": acct["firm_max_drawdown_pct"]})

    # ------------------------------------------------------------------ journal:read

    def query_trades(self, p: Principal, run_id: str, symbol: str | None = None, session: str | None = None,
                     direction: int | None = None, outcome: str | None = None, limit: int = 50) -> dict:
        p.require("journal:read")
        t = self._trades(run_id)
        if symbol:
            t = t[t["symbol"] == symbol.upper()]
        if session:
            t = t[metrics.session_label(pd.DatetimeIndex(t["entry_time"])) == session]
        if direction in (1, -1):
            t = t[t["direction"] == direction]
        if outcome == "win":
            t = t[t["r"] > 0]
        elif outcome == "loss":
            t = t[t["r"] <= 0]
        elif outcome is not None:
            raise BadRequest("outcome must be 'win' or 'loss'")
        cols = ["symbol", "direction", "entry_time", "exit_time", "exit_reason", "r", "cost_r", "mfe_r", "mae_r"]
        cols = [c for c in cols if c in t.columns]
        lim = _clamp(limit, 1, MAX_TRADES)
        rows = t[cols].tail(lim)
        return _jsonable({"run_id": run_id, "matched": len(t), "returned": len(rows), "columns": cols,
                          "rows": rows.astype(object).where(rows.notna(), None).values.tolist()})

    def mfe_mae(self, p: Principal, run_id: str, group_by: str = "exit_reason") -> dict:
        p.require("journal:read")
        t = self._trades(run_id)
        if not {"mfe_r", "mae_r"} <= set(t.columns):
            raise BadRequest("this run did not record MFE/MAE")
        key = self._group_key(t, group_by)
        g = t.groupby(key)
        out = pd.DataFrame({
            "trades": g.size(), "expectancy_r": g["r"].mean(),
            "mfe_r_median": g["mfe_r"].median(), "mfe_r_p75": g["mfe_r"].quantile(0.75),
            "mae_r_median": g["mae_r"].median(),
            "losers_reaching_1r": g.apply(lambda d: float(((d["r"] <= 0) & (d["mfe_r"] >= 1.0)).mean())),
        }).round(3)
        return {"run_id": run_id, "group_by": group_by, "groups": _jsonable(out.to_dict("index"))}

    def loss_clusters(self, p: Principal, run_id: str) -> dict:
        p.require("journal:read")
        t = self._trades(run_id).sort_values("entry_time")
        if t.empty:
            return {"run_id": run_id, "trades": 0}
        entry = pd.DatetimeIndex(t["entry_time"])
        loss = (t["r"] <= 0).to_numpy()
        streaks, cur = [], 0
        for is_loss in loss:
            cur = cur + 1 if is_loss else 0
            streaks.append(cur)
        by = {}
        for name, key in (("session", metrics.session_label(entry)), ("weekday", entry.day_name()),
                          ("hour_utc", entry.hour), ("symbol", t["symbol"].to_numpy())):
            g = pd.DataFrame({"k": key, "loss": loss, "r": t["r"].to_numpy()}).groupby("k")
            d = pd.DataFrame({"trades": g.size(), "loss_rate": g["loss"].mean(), "total_r": g["r"].sum()})
            by[name] = _jsonable(d.sort_values("total_r").head(5).round(3).to_dict("index"))
        return {"run_id": run_id, "trades": len(t), "loss_rate": round(float(loss.mean()), 3),
                "longest_losing_streak": int(max(streaks)), "worst_groups": by}

    # ------------------------------------------------------------------ performance:read

    def performance_summary(self, p: Principal, run_id: str, by: str | None = None) -> dict:
        p.require("performance:read")
        t = self._trades(run_id)
        out = {"run_id": run_id, "overall": metrics.summary(t)}
        if by:
            if by not in ("year", "session", "symbol"):
                raise BadRequest("by must be 'year', 'session' or 'symbol'")
            out["by_" + by] = metrics.breakdown(t, by).round(4).to_dict("index")
        return _jsonable(out)

    # ------------------------------------------------------------------ helpers

    def _strategy(self, name: str) -> dict:
        if name not in self.cfg["strategies"] or self.cfg["strategies"][name].get("setup", name) not in SETUPS:
            raise BadRequest(f"unknown strategy {name!r}; configured: {', '.join(self.cfg['strategies'])}")
        return self.cfg["strategies"][name]

    def _budget(self, strategy: str) -> None:
        self.registry.check_budget(strategy, self.cfg["budget"]["experiments_per_strategy_per_month"], self._now())

    def _exits(self) -> ExitPolicy:
        return ExitPolicy(rr=self.cfg["exits"]["rr"], friday_flatten_utc=self.cfg["exits"]["friday_flatten_utc"])

    def _run_id(self, strategy, params, start, end, mult) -> str:
        h = hashlib.sha256(json.dumps([strategy, params, str(start), str(end), mult, self._now().isoformat()],
                                      sort_keys=True, default=str).encode()).hexdigest()[:8]
        return f"{strategy}-bt-{self._now():%Y%m%d%H%M%S}-{h}"

    def _run_dir(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            raise BadRequest("bad run_id")
        d = self.runs_dir / run_id
        if not d.is_dir():
            raise NotFound(f"no recorded run {run_id!r}")
        return d

    def _save_run(self, run_id: str, summary: dict, trades: pd.DataFrame) -> None:
        d = self.runs_dir / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "summary.json").write_text(json.dumps(_jsonable(summary), indent=2, sort_keys=True))
        trades.to_csv(d / "trades.csv", index=False)

    def _load_summary(self, run_id: str) -> dict:
        path = self._run_dir(run_id) / "summary.json"
        if not path.exists():
            raise NotFound(f"run {run_id!r} has no summary")
        summary = json.loads(path.read_text())
        if self._is_holdout_record(summary):
            raise HoldoutRefused(f"run {run_id!r} is a holdout result; it can only be read outside the agent runtime")
        self._trades(run_id, required=False)  # refuses runs whose trades reach the holdout
        return summary

    def _is_holdout_record(self, rec: dict) -> bool:
        """A registry entry or summary that holds holdout results (T6 writes these outside the agent runtime)."""
        if "holdout" in str(rec.get("kind", "")).lower() or "holdout" in rec:
            return True
        window = rec.get("window")
        if isinstance(window, list) and len(window) == 2:
            try:
                return _utc(window[1]) > self.holdout_start
            except (ValueError, TypeError):
                return True
        return False

    def _trades(self, run_id: str, required: bool = True) -> pd.DataFrame | None:
        d = self._run_dir(run_id)
        path = next((d / n for n in ("trades.csv", "oos_trades.csv") if (d / n).exists()), None)
        if path is None:
            if not required:
                return None
            raise NotFound(f"run {run_id!r} has no trade list")
        t = pd.read_csv(path)
        for c in ("entry_time", "exit_time", "decision_time"):
            if c in t.columns:
                t[c] = pd.to_datetime(t[c], utc=True, format="ISO8601")
        if len(t) and pd.DatetimeIndex(t["exit_time"]).max() > self.holdout_start:
            raise HoldoutRefused(f"run {run_id!r} contains holdout trades; it can only be read outside the agent runtime")
        return t

    def _group_key(self, t: pd.DataFrame, by: str):
        if by == "session":
            return metrics.session_label(pd.DatetimeIndex(t["entry_time"]))
        if by in ("exit_reason", "symbol", "direction", "setup"):
            return t[by]
        raise BadRequest("group_by must be exit_reason, session, symbol, direction or setup")


def _clamp(v, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        raise BadRequest(f"expected an integer, got {v!r}") from None


def _num(v, nd: int = 3):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else round(f, nd)


def _jsonable(o):
    """Plain JSON types only (numpy, pandas, inf/nan -> None or str)."""
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return None if np.isnan(f) else ("inf" if np.isinf(f) else f)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, dt.datetime, dt.date)):
        return str(o)
    return o


__all__ = ["ResearchService", "HoldoutRefused", "BadRequest", "NotFound", "BudgetExceeded"]
