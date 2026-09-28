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


@pytest.fixture
def feed_server(monkeypatch):
    """Local HTTP/1.1 server standing in for the Dukascopy feed."""
    import http.server
    import threading

    state = {"connections": 0, "limited": 2, "requests": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            state["connections"] += 1
            super().setup()

        def do_GET(self):
            state["requests"].append(self.path)
            if self.path.startswith("/limited") and state["limited"] > 0:
                state["limited"] -= 1
                code, body = 429, b'{"error": "Too Many Requests"}'
            elif self.path.startswith("/missing"):
                code, body = 404, b""
            else:
                code, body = 200, self.path.encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(dukascopy.time, "sleep", lambda s: None)
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()


def test_fetch_reuses_one_connection(feed_server):
    base, state = feed_server
    bodies = [dukascopy.fetch(f"{base}/day{i}.bi5") for i in range(5)]
    assert bodies == [f"/day{i}.bi5".encode() for i in range(5)]
    assert state["connections"] == 1


def test_fetch_waits_out_rate_limit_and_maps_404(feed_server):
    base, state = feed_server
    assert dukascopy.fetch(f"{base}/limited.bi5") == b"/limited.bi5"
    assert state["requests"].count("/limited.bi5") == 3
    assert dukascopy.fetch(f"{base}/missing.bi5") == b""


def test_fetch_gives_up_when_always_rate_limited(feed_server):
    base, state = feed_server
    state["limited"] = 100
    with pytest.raises(RuntimeError, match="rate limited"):
        dukascopy.fetch(f"{base}/limited.bi5", rate_limit_retries=3)
    assert state["requests"].count("/limited.bi5") == 4


def test_fetch_retries_a_dropped_keep_alive_connection(feed_server, monkeypatch):
    base, state = feed_server
    assert dukascopy.fetch(f"{base}/a.bi5") == b"/a.bi5"
    port = int(base.rsplit(":", 1)[1])
    conn = next(c for (_, _, p), c in dukascopy._local.conns.items() if p == port)
    real = conn.getresponse
    calls = {"n": 0}

    def drop_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise dukascopy.http.client.RemoteDisconnected("closed")
        return real()

    monkeypatch.setattr(conn, "getresponse", drop_once)
    assert dukascopy.fetch(f"{base}/b.bi5", retries=0) == b"/b.bi5"


def test_download_range_finishes_other_files_before_reporting_failures(tmp_path, monkeypatch):
    def fake_fetch(url):
        if "/02/" in url and "BID" in url:
            raise RuntimeError(f"failed to fetch {url}: boom")
        return b""

    monkeypatch.setattr(dukascopy, "fetch", fake_fetch)
    with pytest.raises(RuntimeError, match="1 day-files failed"):
        dukascopy.download_range("EURUSD", dt.date(2020, 1, 1), dt.date(2020, 1, 3), tmp_path, workers=2)
    assert len(list(tmp_path.rglob("*.bi5"))) == 5
