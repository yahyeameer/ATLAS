"""Jev adapter contract (PRD §17): leakage guard, pinned versions, skip-never-guess, caching."""

import time

import pytest

from atlas_engine.adapters.jev import JevAdapter, JevResult, LeakageError, ReplayTransport, Skip, request_hash
from atlas_engine.adapters.jev.adapter import _cache_key

STATE = {
    "stop_atr": 1.2, "spread_r": 0.05, "dist_ema_atr": 0.3, "h1_fast_dist": 0.8, "h1_slow_dist": 1.5,
    "h1_slope_atr": 0.02, "h1_adx": 27.0, "htf_aligned": 1.0, "atr_pct": 0.5, "atr_ratio": 0.45,
    "room_prior_day_atr": 3.0, "behind_prior_day_atr": 2.0, "ret_1_atr": -0.2, "ret_4_atr": -0.6, "ret_16_atr": 1.1,
    "session": "london", "regime": "trend_normal_vol", "setup": "trend_pullback",
}
MODEL = "jev-2026.09.1"


def answer(req, **over):
    return {"setup_id": req["setup_id"], "p_target_first": 0.58, "regime": "trend_normal_vol",
            "reason_codes": ["htf_aligned", "clean_pullback"], "model_version": MODEL, **over}


def adapter(fn, **kw):
    return JevAdapter(fn, MODEL, **kw)


def test_valid_answer_passes_through():
    res = adapter(answer).evaluate_setup("SETUP-1", STATE, 2.0, 0.08)
    assert isinstance(res, JevResult) and res.p_target_first == 0.58 and res.regime == "trend_normal_vol"


@pytest.mark.parametrize("extra", [
    {"decision_time": "2024-03-01T10:00:00Z"},
    {"close": 1.0843},
    {"symbol": "EURUSD"},
    {"balance": 100_000},
])
def test_leaking_fields_are_refused_before_sending(extra):
    sent = []
    with pytest.raises(LeakageError):
        adapter(lambda r: sent.append(r) or answer(r)).evaluate_setup("S", {**STATE, **extra}, 2.0, 0.08)
    assert sent == []


@pytest.mark.parametrize("key,value", [("session", "2024-01-02"), ("regime", "bull"), ("setup", "eurusd_breakout"), ("h1_adx", "27")])
def test_bad_values_are_refused(key, value):
    with pytest.raises(LeakageError):
        adapter(answer).build_request("S", {**STATE, key: value}, 2.0, 0.08)


def test_request_carries_pinned_wording_and_no_account_data():
    req = adapter(answer).build_request("S", STATE, 2.0, 0.08)
    assert req["questions_version"] and "target" in req["questions"]["p_target_first"]
    assert set(req) == {"setup_id", "model_version", "questions_version", "questions", "state"}
    assert "balance" not in str(req) and "equity" not in str(req)


def test_live_refuses_unpinned_model():
    with pytest.raises(ValueError):
        JevAdapter(answer, "jev-latest", live=True)
    with pytest.raises(ValueError):
        JevAdapter(answer, "", live=True)
    JevAdapter(answer, MODEL, live=True)


def test_timeout_is_a_skip():
    def slow(req):
        time.sleep(0.3)
        return answer(req)

    res = adapter(slow, timeout_ms=50).evaluate_setup("S", STATE, 2.0, 0.08)
    assert isinstance(res, Skip) and res.reason == "timeout"


@pytest.mark.parametrize("over,reason", [
    ({"p_target_first": 1.2}, "out_of_range:p_target_first"),
    ({"p_target_first": "0.6"}, "schema:p_target_first"),
    ({"p_target_first": float("nan")}, "schema:p_target_first"),
    ({"regime": "moon"}, "schema:regime"),
    ({"model_version": "jev-2026.10.0"}, "schema:model_version_mismatch"),
    ({"setup_id": "OTHER"}, "schema:setup_id_mismatch"),
    ({"reason_codes": ["Buy EURUSD now!"]}, "schema:reason_codes"),
])
def test_invalid_answers_are_skips(over, reason):
    res = adapter(lambda r: answer(r, **over)).evaluate_setup("S", STATE, 2.0, 0.08)
    assert isinstance(res, Skip) and res.reason == reason


def test_transport_error_is_a_skip():
    def boom(req):
        raise ConnectionError("down")

    res = adapter(boom).evaluate_setup("S", STATE, 2.0, 0.08)
    assert isinstance(res, Skip) and res.reason == "transport_error:ConnectionError"


def test_same_state_is_answered_from_cache():
    calls = []
    a = adapter(lambda r: calls.append(1) or answer(r))
    first = a.evaluate_setup("S1", STATE, 2.0, 0.08)
    second = a.evaluate_setup("S2", STATE, 2.0, 0.08)
    assert len(calls) == 1 and second.cached and second.p_target_first == first.p_target_first and second.setup_id == "S2"
    a.evaluate_setup("S3", {**STATE, "h1_adx": 30.0}, 2.0, 0.08)
    assert len(calls) == 2


def test_replay_transport_answers_recorded_states_only():
    a = adapter(None)
    req = a.build_request("S", STATE, 2.0, 0.08)
    a.transport = ReplayTransport({request_hash(_cache_key(req)): {**answer(req), "setup_id": "S"}})
    assert isinstance(a.evaluate_setup("S", STATE, 2.0, 0.08), JevResult)
    miss = a.evaluate_setup("S", {**STATE, "h1_adx": 11.0}, 2.0, 0.08)
    assert isinstance(miss, Skip) and miss.reason == "transport_error:KeyError"
