"""T3 exit gate (PRD §26): "100% risk tests pass; breach < 2%".

    python tests/engine/t3_gate.py [--sims 5000] [--out t3_gate.json]

1. Runs the risk test suite (tests/engine) and requires every test to pass.
2. Runs the prop evaluation Monte Carlo (atlas_research.prop_sim) through the
   risk engine with the repo's config/ on synthetic reference trade profiles,
   at the configured risk and at the 0.75% evaluation ceiling (PRD §20), and
   requires P(firm breach) < 2% for each. The same runs without the engine
   show what the internal limits buy.

The profiles are synthetic trade lists with a known expectancy, not market
data. Once T0 has a candidate, run ``atlas-research prop-mc --trades`` on its
out-of-sample trades; that is the number that decides risk for T8.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas_engine.config import load_engine_config, validate  # noqa: E402
from atlas_research import prop_sim  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=5_000)
    ap.add_argument("--horizon-days", type=int, default=60)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tests = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(ROOT / "tests" / "engine")],
                           cwd=ROOT, capture_output=True, text=True)
    summary = tests.stdout.strip().splitlines()[-1] if tests.stdout.strip() else tests.stderr[-500:]
    print(f"risk tests: {summary}")

    base = load_engine_config(ROOT / "config")
    stress_risk = dataclasses.replace(base.risk, risk_per_trade_pct=0.75, one_bet_max_risk_pct=0.75)
    stress = dataclasses.replace(base, risk=stress_risk)
    validate(stress)
    rows = []
    for label, cfg in ((f"{base.risk.risk_per_trade_pct:.2f}%", base), ("0.75%", stress)):
        for profile in prop_sim.PROFILES:
            trades = prop_sim.reference_trades(profile)
            eng = prop_sim.simulate_evaluation(trades, cfg, args.sims, args.horizon_days, use_engine=True)
            raw = prop_sim.simulate_evaluation(trades, cfg, args.sims, args.horizon_days, use_engine=False)
            rows.append({"risk": label, "profile": profile, "expectancy_r": round(float(trades["r"].mean()), 3),
                         "engine": eng, "firm_rules_only": raw, "passed": eng["p_firm_breach"] < 0.02})
            print(f"{label:>6} {profile:<15} breach {eng['p_firm_breach']:.2%} (no engine {raw['p_firm_breach']:.2%})  "
                  f"pass {eng['p_passed']:.1%} (no engine {raw['p_passed']:.1%})  "
                  f"dd-stop {eng['p_drawdown_stop']:.1%}  blocked {eng['entries_blocked_share']:.1%}")

    ok = tests.returncode == 0 and all(r["passed"] for r in rows)
    result = {"tests": summary, "tests_passed": tests.returncode == 0, "sims": args.sims,
              "horizon_days": args.horizon_days, "config_checksum": base.checksum, "runs": rows, "passed": ok}
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, default=str))
    print("T3 gate:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
