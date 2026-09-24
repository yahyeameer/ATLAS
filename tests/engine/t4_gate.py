"""T4 exit gate (PRD §26): "failure-injection suite passes on demo".

    python tests/engine/t4_gate.py [--out t4_gate.json]

Runs every failure-injection scenario in t4_scenarios.py against the engine
on the fake MT5 terminal and prints one line per scenario. The gate here is
the fake-terminal half. The PRD asks for the same suite on a demo account;
that half needs the Windows VPS, a demo login and the compiled watchdog EA,
and is listed in docs/t4-engine-and-mt5.md. Until it has been run, T4 is
not done.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1]), str(HERE)]

from t4_scenarios import SCENARIOS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    rows = []
    for name, ref, fn in SCENARIOS:
        t = time.perf_counter()
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
                ok, err = True, ""
            except Exception as e:  # noqa: BLE001
                ok, err = False, "".join(traceback.format_exception_only(type(e), e)).strip()
        rows.append({"scenario": fn.__name__, "name": name, "prd": ref, "passed": ok, "error": err,
                     "seconds": round(time.perf_counter() - t, 3)})
        print(f"{'PASS' if ok else 'FAIL'}  {ref:<10} {name}" + (f"\n      {err}" if err else ""))
    passed = sum(r["passed"] for r in rows)
    print(f"\n{passed}/{len(rows)} scenarios pass on the fake MT5 terminal. The demo-account run is still owed.")
    if args.out:
        Path(args.out).write_text(json.dumps({"broker": "fake-mt5", "scenarios": rows}, indent=2))
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
