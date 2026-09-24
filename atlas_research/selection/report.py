"""Write a T2 run's artifacts: summary JSON, per-arm OOS trades, candidates and a Markdown report."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ..report import _fmt, _table


def write_report(run_dir: Path, result: dict, trades: dict[str, pd.DataFrame], candidates: pd.DataFrame) -> list[str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.json"
    report_path = run_dir / "report.md"
    cand_path = run_dir / "candidates.csv"
    summary_path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str))
    candidates.to_csv(cand_path, index=False)
    paths = [str(report_path), str(summary_path), str(cand_path)]
    for arm, t in trades.items():
        p = run_dir / f"oos_trades_{arm.replace('+', '_')}.csv"
        t.to_csv(p, index=False)
        paths.append(str(p))

    cols = ["trades", "expectancy_r", "profit_factor", "win_rate", "kept_fraction", "p_beats_rules", "kept"]
    arms = pd.DataFrame({k: {c: v.get(c) for c in cols} for k, v in result["arms"].items()}).T
    checks = pd.DataFrame({k: v["checks"] for k, v in result["arms"].items() if "checks" in v}).T
    cal = pd.DataFrame({k: {c: v["calibration"][c] for c in ("n", "brier", "brier_base_rate", "brier_skill")}
                        for k, v in result["arms"].items() if "calibration" in v}).T
    groups = [pd.DataFrame(v["calibration"]["by_group"]).assign(arm=k) for k, v in result["arms"].items() if "calibration" in v]
    groups = pd.concat(groups, ignore_index=True).set_index(["arm", "symbol", "session"]) if any(len(g) for g in groups) else pd.DataFrame()
    verdict = f"best arm {result['best_arm']} {'KEPT' if result['passed'] else 'not kept'}"
    md = [
        f"# T2 {result['setup']} selection: {verdict}",
        "",
        f"- Experiment: `{result['experiment_id']}` (setup version {result['strategy_version']})",
        f"- Setup parameters ({result['setup_params_source']}): `{json.dumps(result['setup_params'], sort_keys=True)}`",
        f"- Candidates: {result['candidates']['total']} ({result['candidates']['validation']} in validation), "
        f"target-first rate {_fmt(result['candidates']['target_first_rate'])}",
        f"- EV gate: EV_R ≥ {result['ev_min']} R. Dev {result['data_window']['dev'][0]} to {result['data_window']['dev'][1]}, "
        f"validation {result['data_window']['validation'][0]} to {result['data_window']['validation'][1]}; holdout not loaded",
        f"- Deflated Sharpe of the best arm: {_fmt(result['dsr']['value'])} over {result['dsr']['n_trials']} selection trials",
        "",
        "## Arms, all OOS (dev walk-forward + validation), R after costs",
        "",
        _table(arms),
        "## Keep/kill checks",
        "",
        _table(checks),
        "## Calibration (Brier vs the training base rate)",
        "",
        _table(cal),
        "## Calibration by symbol and session",
        "",
        _table(groups),
        "## Segments",
        "",
        _table(pd.DataFrame(result["segments"]["dev_walk_forward"]).T.add_prefix("dev_")[["dev_trades", "dev_expectancy_r"]]
               .join(pd.DataFrame(result["segments"]["validation"]).T.add_prefix("val_")[["val_trades", "val_expectancy_r"]])),
        "## Folds",
        "",
        _table(pd.DataFrame(result["folds"]).set_index("test")),
    ]
    report_path.write_text("\n".join(md))
    return paths
