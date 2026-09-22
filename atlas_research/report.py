"""Write a T0 run's artifacts: summary JSON, trade list and a Markdown report."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd


def _fmt(v) -> str:
    if isinstance(v, float):
        if math.isinf(v):
            return "inf"
        return f"{v:.3f}"
    return str(v)


def _table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_none_\n"
    d = df.reset_index()
    head = "| " + " | ".join(map(str, d.columns)) + " |"
    sep = "| " + " | ".join("---" for _ in d.columns) + " |"
    rows = ["| " + " | ".join(_fmt(v) for v in r) + " |" for r in d.itertuples(index=False)]
    return "\n".join([head, sep, *rows]) + "\n"


def write_report(run_dir: Path, result: dict, oos: pd.DataFrame, years: pd.DataFrame, sessions: pd.DataFrame, symbols: pd.DataFrame) -> list[str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.json"
    trades_path = run_dir / "oos_trades.csv"
    report_path = run_dir / "report.md"
    summary_path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str))
    oos.to_csv(trades_path, index=False)

    gates = pd.DataFrame(result["gates"])[["gate", "scope", "value", "rule", "passed", "source"]].set_index("gate")
    folds = pd.DataFrame(result["walk_forward"]["folds"]).set_index("test")
    verdict = "PASSED every gate" if result["passed"] else f"FAILED {int((~gates['passed']).sum())} of {len(gates)} gates"
    md = [
        f"# T0 {result['strategy']} — {verdict}",
        "",
        f"- Experiment: `{result['experiment_id']}` (strategy version {result['strategy_version']})",
        f"- Hypothesis: {result['hypothesis']}",
        f"- Symbols: {', '.join(result['symbols'])}",
        f"- Dev {result['data_window']['dev'][0]} to {result['data_window']['dev'][1]}, validation {result['data_window']['validation'][0]} to {result['data_window']['validation'][1]}; holdout not loaded",
        f"- Final parameters: `{json.dumps(result['final_params'], sort_keys=True)}`",
        "",
        "## Gates",
        "",
        _table(gates),
        "## Segments",
        "",
        _table(pd.DataFrame({"dev walk-forward OOS": result["dev_oos"], "validation": result["validation"]})),
        "## Walk-forward folds",
        "",
        _table(folds),
        "## By year (all OOS)",
        "",
        _table(years),
        "## By session (all OOS)",
        "",
        _table(sessions),
        "## By symbol (all OOS)",
        "",
        _table(symbols),
        "## Robustness",
        "",
        f"- Random-entry control: {result['random_control']['runs']} runs, mean {result['random_control']['mean']:.3f} R, p95 {result['random_control']['p95']:.3f} R",
        f"- ±20% neighbours on validation: {', '.join(f'{x:.3f}' for x in result['neighbours']) or 'none'}",
        f"- Deflated Sharpe counts {result['dsr']['n_trials']} trials (variance of trial Sharpes {result['dsr']['var_trials']:.5f})",
        "",
    ]
    report_path.write_text("\n".join(md))
    return [str(report_path), str(summary_path), str(trades_path)]
