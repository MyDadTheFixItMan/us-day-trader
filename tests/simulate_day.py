"""Runs trader.py through a whole fake trading day, every 5 minutes, with made-up prices.

python tests/simulate_day.py   (uses a temp copy of the repo; real data is untouched)
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
TZ = "America/New_York"
DAY = "2026-11-04"  # a Wednesday


def make_bars(seed, start, drift, vol_mult, or_bias, pop=0.0):
    rnd = random.Random(seed)
    px, out = start, []
    t = pd.Timestamp(f"{DAY} 09:30", tz=TZ)
    while t.time() < pd.Timestamp("16:00").time():
        mins = t.hour * 60 + t.minute
        d = or_bias if mins < 585 else (pop if mins < 610 else drift)
        o = px
        c = max(0.05, o * (1 + d + rnd.gauss(0, 0.002)))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.001)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, 0.001)))
        v = (30000 if mins < 585 else 10000) * vol_mult
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
        px, t = c, t + pd.Timedelta(minutes=5)
    return out


SCENARIO = {
    "RUNR": make_bars(1, 25.00, 0.0015, 4, 0.002),    # in play, trends up all day
    "FADE": make_bars(2, 12.50, -0.003, 3, 0.003, pop=0.004),    # in play, breaks out then fails
    "SLOW": make_bars(3, 40.0, 0.001, 1, 0.001),     # normal volume -> not in play
    "DOWN": make_bars(4, 15.00, 0.0, 5, -0.003),      # high volume but red open -> skipped
    "^GSPC": make_bars(5, 6800, 0.0001, 1, 0.0002),
}
UNIVERSE = {
    "date": DAY, "scanned": 2100, "priced": 1900,
    "symbols": {
        "RUNR": {"prev_close": 24.75, "atr": 0.75, "avg_turnover": 5e6, "avg_or_volume": 90000 * 4 / 3},
        "FADE": {"prev_close": 12.25, "atr": 0.40, "avg_turnover": 3e6, "avg_or_volume": 90000 * 3 / 3},
        "SLOW": {"prev_close": 39.80, "atr": 1.00, "avg_turnover": 8e6, "avg_or_volume": 90000},
        "DOWN": {"prev_close": 15.25, "atr": 0.50, "avg_turnover": 2e6, "avg_or_volume": 90000},
        "__index__": {"prev_close": 6790},
    },
}

FAKE_DATA = '''
import json, pandas as pd
from pathlib import Path
SC = json.loads(Path(__file__).with_name("scenario.json").read_text())
UNI = json.loads(Path(__file__).with_name("uni.json").read_text())
def us_codes(path): return ["X"] * 1500
def build_universe(codes, cfg, today): return UNI
def intraday_bars(tickers, now, delay):
    cutoff = now - pd.Timedelta(minutes=5 + delay)
    out = {}
    for t in tickers:
        k = t
        bars = [dict(b, t=pd.Timestamp(b["t"])) for b in SC.get(k, [])]
        bars = [b for b in bars if b["t"] <= cutoff]
        if bars: out[k] = bars
    return out
'''


def main():
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(REPO / "bot", tmp / "bot")
    (tmp / "data").mkdir()
    (tmp / "bot" / "data.py").write_text(FAKE_DATA)
    cfgp = tmp / "bot" / "config.json"   # no network in tests: switch live news off
    cfg = json.loads(cfgp.read_text()); cfg["news_enabled"] = False; cfgp.write_text(json.dumps(cfg))
    (tmp / "bot" / "scenario.json").write_text(json.dumps(
        {k: [dict(b, t=b["t"].isoformat()) for b in v] for k, v in SCENARIO.items()}))
    (tmp / "bot" / "uni.json").write_text(json.dumps(UNIVERSE))

    t = pd.Timestamp(f"{DAY} 09:30", tz=TZ)
    while t <= pd.Timestamp(f"{DAY} 17:00", tz=TZ):
        env = dict(os.environ, TRADER_NOW=t.strftime("%Y-%m-%dT%H:%M"))
        r = subprocess.run([sys.executable, "bot/trader.py"], cwd=tmp, env=env, capture_output=True, text=True)
        if r.returncode:
            print(r.stdout, r.stderr)
            raise SystemExit(f"crash at {t}")
        t += pd.Timedelta(minutes=5)

    state = json.loads((tmp / "data/state.json").read_text())
    trades = json.loads((tmp / "data/trades.json").read_text())
    print("\n".join(state["day"]["log"]))
    print("\nStatus:", state["status"])
    print("Cash:", state["cash"], "Positions:", state["positions"])
    for x in trades:
        print(x["sym"], x["qty"], x["entry_price"], "->", x["exit_price"], x["net"], x["r"], x["reason"])
    print("Daily:", state["daily"])
    assert not state["positions"], "should be flat at close"
    assert {x["sym"] for x in trades} <= {"RUNR", "FADE"}, "only in-play stocks traded"
    shutil.copytree(tmp / "data", REPO / "tests" / "sample_data", dirs_exist_ok=True)
    print("OK")


if __name__ == "__main__":
    main()
