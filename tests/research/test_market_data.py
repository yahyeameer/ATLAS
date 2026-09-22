import datetime as dt

import numpy as np
import pandas as pd
import pytest

from atlas_engine.market_data import bars, dukascopy, quality, store, synthetic


def _day_frame(day: dt.date) -> pd.DataFrame:
    idx = pd.date_range(pd.Timestamp(day, tz="UTC"), periods=5, freq="1min", name="time")
    o = np.array([1.10000, 1.10010, 1.10020, 1.10005, 1.09990])
    c = o + 0.00005
    return pd.DataFrame({"open": o, "high": c + 0.00003, "low": o - 0.00004, "close": c, "volume": [1.5, 2, 0, 3, 4]}, index=idx)


def test_candle_url_uses_zero_based_month():
    url = dukascopy.candle_url("eurusd", dt.date(2023, 1, 3), "bid")
    assert url.endswith("/EURUSD/2023/00/03/BID_candles_min_1.bi5")


def test_bi5_round_trip():
    day = dt.date(2023, 3, 6)
    df = _day_frame(day)
    out = dukascopy.decode_candles(dukascopy.encode_candles(df, day, 100_000), day, 100_000)
    pd.testing.assert_frame_equal(out[["open", "high", "low", "close"]], df[["open", "high", "low", "close"]], check_freq=False)
    assert list(out["volume"]) == pytest.approx(list(df["volume"]))


def test_decode_rejects_wrong_field_order():
    day = dt.date(2023, 3, 6)
    df = _day_frame(day)
    swapped = df.rename(columns={"high": "low", "low": "high"})
    with pytest.raises(ValueError, match="OHLC sanity"):
        dukascopy.decode_candles(dukascopy.encode_candles(swapped, day, 100_000), day, 100_000)


def test_empty_file_means_no_data():
    assert dukascopy.decode_candles(b"", dt.date(2023, 3, 4), 100_000).empty


def test_merge_sides_drops_closed_minutes():
    day = dt.date(2023, 3, 6)
    bid = _day_frame(day)
    ask = bid.copy()
    for c in ("open", "high", "low", "close"):
        ask[c] += 0.00002
    ask["volume"] = [1, 1, 0, 1, 1]
    m = bars.merge_sides(bid, ask)
    assert len(m) == 4  # the minute with zero volume on both sides is gone
    assert (m["ask_c"] - m["bid_c"]).round(8).eq(0.00002).all()


def test_resample_is_left_labelled_and_keeps_both_sides():
    m1 = synthetic.random_walk_m1(start="2019-01-07", end="2019-01-08")
    m15 = bars.resample(m1, "15min")
    first = m1.loc[m1.index < m1.index[0] + pd.Timedelta(minutes=15)]
    row = m15.iloc[0]
    assert m15.index[0] == m1.index[0]
    assert row["bid_o"] == first["bid_o"].iloc[0]
    assert row["bid_h"] == first["bid_h"].max()
    assert row["ask_l"] == first["ask_l"].min()
    assert row["ask_c"] == first["ask_c"].iloc[-1]
    assert row["n_sub"] == 15


def test_store_round_trip(tmp_path):
    m1 = synthetic.random_walk_m1(start="2019-12-30", end="2020-01-03")
    store.save_m1(tmp_path, "EURUSD", m1)
    assert (tmp_path / "m1" / "EURUSD" / "2019.parquet").exists()
    assert (tmp_path / "m1" / "EURUSD" / "2020.parquet").exists()
    back = store.load_m1(tmp_path, "EURUSD", pd.Timestamp("2019-12-31", tz="UTC"), pd.Timestamp("2020-01-02", tz="UTC"))
    expected = m1.loc["2019-12-31":"2020-01-01"]
    pd.testing.assert_frame_equal(back, expected, check_freq=False)


def test_quality_report_flags_gaps_and_crossed_quotes():
    m1 = synthetic.random_walk_m1(start="2019-01-07", end="2019-01-09")
    m1 = m1.drop(m1.index[600:620])  # 20-minute hole on Monday
    m1.iloc[700, m1.columns.get_loc("ask_c")] = m1.iloc[700]["bid_c"] - 0.0001
    rep = quality.quality_report(m1, "EURUSD").loc[2019]
    assert rep["gaps_gt_5m"] == 1
    assert rep["crossed_quotes"] >= 1


def test_synthetic_has_no_weekend_bars():
    m1 = synthetic.random_walk_m1(start="2019-01-01", end="2019-02-01")
    assert not (m1.index.weekday == 5).any()


def test_mt5_export_import(tmp_path):
    from atlas_engine.market_data.mt5_csv import read_mt5_bars

    csv = tmp_path / "EURUSD_M1.csv"
    csv.write_text(
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
        "2020.01.07\t12:00:00\t1.10000\t1.10010\t1.09990\t1.10005\t50\t0\t3\n"
        "2020.07.07\t12:00:00\t1.12000\t1.12010\t1.11990\t1.12005\t50\t0\t2\n"
    )
    df = read_mt5_bars(csv, "EURUSD")
    # Server time is New York + 7h: UTC+2 in January, UTC+3 in July.
    assert list(df.index) == [pd.Timestamp("2020-01-07 10:00", tz="UTC"), pd.Timestamp("2020-07-07 09:00", tz="UTC")]
    assert df["ask_o"].iloc[0] - df["bid_o"].iloc[0] == pytest.approx(0.00003)
