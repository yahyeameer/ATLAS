"""The T4 failure-injection suite (scenarios in t4_scenarios.py, shared with t4_gate.py)."""

import pytest

from t4_scenarios import SCENARIOS


@pytest.mark.parametrize("name, ref, scenario", SCENARIOS, ids=[s[2].__name__ for s in SCENARIOS])
def test_scenario(name, ref, scenario, tmp_path):
    scenario(tmp_path)
