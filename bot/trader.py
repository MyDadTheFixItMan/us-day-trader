"""Runs every 5 minutes from GitHub Actions. Usage: python bot/trader.py

Env vars (testing only): TRADER_NOW=2026-11-04T11:00 to fake the clock.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from engine import TZ, Engine, hhmm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG = json.loads((ROOT / "bot" / "config.json").read_text())


def load(name, default):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj):
    DATA.mkdir(exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, indent=1, default=str))


def is_trading_day(d) -> bool:
    if d.weekday() >= 5:
        return False
    try:
        import holidays
        try:
            cal = holidays.financial_holidays("XNYS", years=d.year)
        except Exception:  # noqa: BLE001
            cal = holidays.US(years=d.year)
        return d not in cal
    except ImportError:
        return True


def main():
    now = pd.Timestamp(os.environ["TRADER_NOW"], tz=TZ) if os.environ.get("TRADER_NOW") else pd.Timestamp.now(tz=TZ)
    today = now.date().isoformat()

    state = load("state.json", None) or {
        "start_capital": CFG["start_capital"], "cash": CFG["start_capital"],
        "created": now.isoformat(), "positions": [], "daily": [], "day": {"date": None, "phase": "closed"},
    }
    trades = load("trades.json", [])
    curve = load("equity.json", [])
    eng = Engine(state, trades, CFG)

    def finish(status: str):
        state["status"] = status
        state["last_run"] = now.isoformat()
        save("state.json", state)
        save("trades.json", trades)
        save("equity.json", curve)
        print(status)

    if not is_trading_day(now.date()):
        if state.get("status") != "Market closed today":
            finish("Market closed today")
        return

    if now.time() < hhmm("09:00"):
        print("Too early")
        return

    # ---- new day: morning scan of the S&P 1500 ----
    if state["day"].get("date") != today:
        if state["positions"]:  # should never happen; never hold overnight
            eng.close_day(now)
        import data as md
        uni = load("universe.json", {})
        if uni.get("date") != today:
            codes = md.us_codes(DATA / "us_codes.txt")
            uni = md.build_universe(codes, CFG, now.to_pydatetime())
            save("universe.json", uni)
        eng.new_day(today, uni["symbols"])
        eng.log(now, f"Scanned {uni.get('scanned')} US shares; {len(uni['symbols']) - 1} pass the liquidity and volatility filters.")
        finish("Morning scan done. Waiting for the 9:30 open.")
        return

    day = state["day"]
    if day["phase"] == "closed":
        print("Day already closed")
        return

    if now.time() < hhmm("09:30"):
        print("Waiting for open")
        return

    import data as md
    uni = load("universe.json", {})
    symbols = uni.get("symbols", {})
    delay = CFG["data_delay_minutes"]

    if day["phase"] == "waiting_open":
        tickers = [s for s in symbols if s != "__index__"]
        bars = md.intraday_bars(tickers + [CFG["market_index"]], now, delay)
        index_bars = bars.pop(CFG["market_index"], None)
        if not bars and now.time() > hhmm("10:30"):
            eng.close_day(now)
            finish("No US price data today (market holiday?).")
            return
        if not eng.select_watchlist(bars, index_bars, symbols):
            finish("Building the opening range")
            return
        wanted = {w["sym"] for w in day["watchlist"]}
        bars = {s: b for s, b in bars.items() if s in wanted}
    else:
        wanted = {w["sym"] for w in day["watchlist"]} | {p["sym"] for p in state["positions"]}
        bars = md.intraday_bars(list(wanted), now, delay)

    eng.process(bars)

    # Late-day safety net: flatten if the exit bar never arrived (halt, data gap)
    if day["phase"] == "trading" and now.time() >= hhmm("16:30"):
        last = {s: b[-1]["c"] for s, b in bars.items() if b}
        eng.close_day(now, last)

    # End-of-day review: learn from today's losses
    if day["phase"] == "closed" and not day.get("reviewed"):
        import learn
        learn.review_day(state, trades, CFG)
        eng.log(now, "Daily review done. See 'Learning from losses' on the dashboard.")

    # mark to market + equity curve
    for p in state["positions"]:
        if bars.get(p["sym"]):
            p["last"] = bars[p["sym"]][-1]["c"]
    eq = eng.equity()
    curve.append({"t": now.isoformat(timespec="minutes"), "equity": eq})
    cutoff = (now - pd.Timedelta(days=14)).isoformat()
    curve[:] = [c for c in curve if c["t"] >= cutoff]

    if day["phase"] == "closed":
        status = f"Day finished. Equity ${eq:,.2f}"
    elif day.get("no_new_entries") and not state["positions"]:
        mc = day.get("market_change_pct")
        status = (f"Standing aside today: S&P 500 opened {mc:+.2f}%" if mc is not None and mc < CFG["market_min_change_pct"]
                  else "Done for today: daily loss limit reached")
    elif day.get("no_new_entries"):
        status = "Managing open trades only (no new entries today)"
    elif now.time() >= hhmm(CFG["last_entry_time"]):
        status = "Past the entry cut-off. Managing open trades until the close."
    else:
        status = f"Watching {len(day['watchlist'])} stocks in play for breakouts"
    finish(status)


if __name__ == "__main__":
    main()
