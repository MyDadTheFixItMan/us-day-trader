"""Paper-trading engine: opening range breakout on US stocks in play.

Pure logic with no network access, so it can be tested with made-up bars.
A bar is a dict: {"t": pandas.Timestamp (America/New_York), "o", "h", "l", "c", "v"}.
"""
from __future__ import annotations

import math
from datetime import datetime, time

import pandas as pd

TZ = "America/New_York"
OPEN = "09:30"  # regular session 09:30-16:00 New York
BAR = pd.Timedelta(minutes=5)


# ---------- US price steps (SEC Rule 612: one cent from $1, 0.01 cent below) ----------
def tick_size(price: float) -> float:
    return 0.01 if price >= 1.00 else 0.0001


def round_tick(price: float, direction: str) -> float:
    """Round to a valid US price step: 'up' for buys, 'down' for sells/stops."""
    step = tick_size(price)
    n = price / step
    n = math.ceil(n - 1e-9) if direction == "up" else math.floor(n + 1e-9)
    return round(n * step, 4)


def hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


# ---------- engine ----------
class Engine:
    def __init__(self, state: dict, trades: list, cfg: dict):
        self.s = state
        self.trades = trades
        self.cfg = cfg

    # --- helpers ---
    @property
    def day(self) -> dict:
        return self.s["day"]

    def log(self, t, msg: str):
        stamp = pd.Timestamp(t).tz_convert(TZ).strftime("%H:%M") if t is not None else ""
        self.day.setdefault("log", []).append(f"{stamp} {msg}".strip())

    def fee(self) -> float:
        return float(self.cfg["brokerage_per_trade"])

    def equity(self, prices: dict | None = None) -> float:
        eq = self.s["cash"]
        for p in self.s["positions"]:
            px = (prices or {}).get(p["sym"], p.get("last", p["entry"]))
            eq += p["qty"] * px
        return round(eq, 2)

    # --- day lifecycle ---
    def new_day(self, date_str: str, universe: dict):
        self.s["day"] = {
            "date": date_str,
            "phase": "waiting_open",   # waiting_open -> trading -> closed
            "start_equity": self.equity(),
            "universe_size": len(universe),
            "watchlist": [],
            "entered": [],
            "realized": 0.0,
            "losses_in_row": 0,
            "no_new_entries": False,
            "market_change_pct": None,
            "last_ts": None,
            "log": [],
        }

    def opening_range(self, bars: list) -> dict | None:
        """First N minutes of today's 5-minute bars from the 09:30 open."""
        n = self.cfg["opening_range_minutes"] // 5
        start = hhmm(OPEN)
        orb = [b for b in bars if b["t"].time() >= start][:n]
        if len(orb) < n:
            return None
        # every OR bar must fall inside the window
        end = (datetime.combine(orb[0]["t"].date(), start) + pd.Timedelta(minutes=self.cfg["opening_range_minutes"])).time()
        if any(b["t"].time() >= end for b in orb):
            return None
        return {
            "open": orb[0]["o"],
            "high": max(b["h"] for b in orb),
            "low": min(b["l"] for b in orb),
            "close": orb[-1]["c"],
            "volume": sum(b["v"] for b in orb),
            "end": orb[-1]["t"],
        }

    def select_watchlist(self, bars_by_sym: dict, index_bars: list | None, universe: dict) -> bool:
        """Pick today's stocks in play once the opening range is complete. Returns True when done."""
        cfg = self.cfg
        # Market filter: long-only, so stand aside when the whole market opens weak.
        if index_bars:
            idx_or = self.opening_range(index_bars)
            if idx_or is None:
                return False
            prev = universe.get("__index__", {}).get("prev_close")
            if prev:
                chg = (idx_or["close"] / prev - 1) * 100
                self.day["market_change_pct"] = round(chg, 2)
                if chg < cfg["market_min_change_pct"]:
                    self.day["watchlist"] = []
                    self.day["phase"] = "trading"
                    self.day["no_new_entries"] = True
                    self.log(idx_or["end"], f"S&P 500 {chg:+.2f}% at the open: weak market, standing aside today.")
                    return True

        # Tudor Jones / O'Neil "M": long only when the market is in an uptrend
        mma = cfg.get("market_trend_ma")
        im = universe.get("__index__", {})
        if mma and im.get(f"ma{mma}") and im.get("prev_close") and im["prev_close"] < im[f"ma{mma}"]:
            self.day["watchlist"] = []
            self.day["phase"] = "trading"
            self.day["no_new_entries"] = True
            self.log(None, f"S&P 500 is below its {mma}-day average: market downtrend, standing aside today.")
            return True

        cands = []
        ready = 0
        for sym, bars in bars_by_sym.items():
            meta = universe.get(sym)
            if not meta:
                continue
            orr = self.opening_range(bars)
            if orr is None:
                continue
            ready += 1
            avg_or_vol = meta.get("avg_or_volume") or 0
            if avg_or_vol <= 0:
                continue
            rvol = orr["volume"] / avg_or_vol
            bullish = orr["close"] > orr["open"]
            above_prev = orr["close"] > meta.get("prev_close", 0)
            # Lesson from JNS: when one price step is a big % of the price, slippage eats the edge.
            step_ok = tick_size(orr["close"]) / orr["close"] * 100 <= cfg.get("max_tick_pct", 100)
            gap_ok = orr["close"] <= cfg.get("max_price", 1e12)
            if cfg.get("min_gap_pct") and meta.get("prev_close"):
                gap_ok = gap_ok and (orr["open"] / meta["prev_close"] - 1) * 100 >= cfg["min_gap_pct"]
            # Book filters on the stock itself (only facts known before the open)
            sma = cfg.get("stock_trend_ma")
            if sma and not (meta.get(f"ma{sma}") and meta.get("prev_close", 0) > meta[f"ma{sma}"]):
                continue  # Murphy / Pring: stock must be in its own uptrend
            if cfg.get("near_high_pct") is not None and not (meta.get("high252") and meta.get("prev_close", 0) >= meta["high252"] * (1 - cfg["near_high_pct"] / 100)):
                continue  # O'Neil / Livermore: leaders trade near their highs
            if cfg.get("rs_min") is not None and (meta.get("ret63") is None or im.get("ret63") is None or meta["ret63"] - im["ret63"] < cfg["rs_min"]):
                continue  # O'Neil: relative strength vs the market over ~3 months
            if cfg.get("or_close_top_pct") and orr["high"] > orr["low"] and \
                    (orr["close"] - orr["low"]) / (orr["high"] - orr["low"]) * 100 < 100 - cfg["or_close_top_pct"]:
                continue  # Nison: strong opening candle (closes near its high)
            nw = (self.day.get("news") or {}).get(sym)
            if cfg.get("news_require") and not nw:
                continue  # Cameron's catalyst pillar: only shares with fresh price-sensitive news
            if cfg.get("news_learning") and nw and nw["category"] in (self.day.get("news_rules") or {}).get("block", []):
                if rvol >= cfg["min_relative_volume"] and bullish and above_prev:
                    self.log(None, f"Skipped {sym}: '{nw['category']}' news has tended to fall after an up-open.")
                continue
            gap_now = (orr["open"] / meta["prev_close"] - 1) * 100 if meta.get("prev_close") else None
            if cfg.get("max_gap_pct") is not None:
                # Gap-down setups (Chan's buy-on-gap; Griffith study of large ASX stocks rebounding after bad nights)
                gap_ok = gap_ok and gap_now is not None and gap_now <= cfg["max_gap_pct"]
            if not cfg.get("require_above_prev", True):
                above_prev = True
            if not cfg.get("require_green_or", True):
                bullish = True
            if rvol >= cfg["min_relative_volume"] and bullish and above_prev and step_ok and gap_ok:
                cands.append({
                    "sym": sym,
                    "rvol": round(rvol, 2),
                    "or_high": orr["high"],
                    "or_low": orr["low"],
                    "or_open": orr["open"],
                    "or_close": orr["close"],
                    "atr": meta["atr"],
                    "gap_pct": round((orr["open"] / meta["prev_close"] - 1) * 100, 2) if meta.get("prev_close") else None,
                    "news": nw,
                })
        if ready == 0:
            return False
        boost = set((self.day.get("news_rules") or {}).get("boost", [])) if cfg.get("news_learning") else set()
        cands.sort(key=lambda c: c["rvol"] * (1.5 if c.get("news") and c["news"]["category"] in boost else 1.0), reverse=True)
        self.day["watchlist"] = cands[: cfg["watchlist_size"]]
        self.day["phase"] = "trading"
        if self.day["watchlist"]:
            names = ", ".join(f"{c['sym']} ({c['rvol']}x" + (f", news: {c['news']['category']}" if c.get("news") else "") + ")"
                              for c in self.day["watchlist"])
            self.log(None, f"Stocks in play: {names}")
        else:
            self.log(None, "No stock met the in-play rules today. No trades.")
        return True

    # --- trading ---
    def _risk_budget(self) -> float:
        mult = (self.s.get("learning") or {}).get("risk_multiplier", 1.0)
        return self.day["start_equity"] * self.cfg["risk_per_trade_pct"] / 100 * mult

    def _check_halt(self, t, unrealized: float):
        if self.day["no_new_entries"]:
            return
        limit = -self.day["start_equity"] * self.cfg["daily_loss_limit_pct"] / 100
        if self.day["realized"] + unrealized <= limit:
            self.day["no_new_entries"] = True
            self.log(t, "Daily loss limit reached. No more entries today.")
        elif self.day["losses_in_row"] >= self.cfg["max_losses_in_row"]:
            self.day["no_new_entries"] = True
            self.log(t, f"{self.day['losses_in_row']} losses in a row. Stopping for the day.")

    def _enter(self, w: dict, bar: dict):
        cfg = self.cfg
        slip = cfg["slippage_pct"] / 100
        if cfg.get("entry_confirm_close"):
            # Lesson from false breakouts: only buy once a 5-minute bar CLOSES above the range.
            if bar["c"] <= w["or_high"]:
                return
            raw = bar["c"] * (1 + slip)
        elif cfg.get("entry_mode") == "after_or":
            raw = bar["o"] * (1 + slip)  # buy as soon as the opening range is complete
        else:
            trigger = w["or_high"] + tick_size(w["or_high"])
            raw = max(trigger, bar["o"]) * (1 + slip)
        price = round_tick(raw, "up")
        if cfg.get("stop_mode") == "low_of_day":
            # Kullamagi episodic pivot: stop at the low of the day
            stop = self.day.get("tracks", {}).get(w["sym"], {}).get("low", w["or_low"])
        elif not cfg.get("stop_use_or_low", True):
            stop = price - cfg["stop_atr_fraction"] * w["atr"]
        else:
            stop = max(w["or_low"], price - cfg["stop_atr_fraction"] * w["atr"])
        stop = min(stop, price * (1 - cfg["min_stop_pct"] / 100))
        stop = round_tick(stop, "down")
        risk_ps = price - stop
        if risk_ps <= 0:
            return
        if cfg.get("max_stop_atr") and risk_ps > cfg["max_stop_atr"] * w["atr"]:
            self.log(bar["t"], f"Skipped {w['sym']}: stop would be more than {cfg['max_stop_atr']} ATR away.")
            w["skipped"] = True
            return
        cand = {"entry_price": price, "entry_time": bar["t"].isoformat(), "gap_pct": w.get("gap_pct"),
                "rvol": w["rvol"], "stop_pct": round(risk_ps / price * 100, 2),
                "market_pct": self.day.get("market_change_pct")}
        from learn import blocked_reason
        why = blocked_reason(self.s, cand)
        if why:
            self.log(bar["t"], f"Skipped {w['sym']}: {why}.")
            w["skipped"] = True
            return
        # Lesson from losses bigger than planned: size the trade so slippage and both fees fit inside the risk budget.
        cost_ps = risk_ps + (price + stop) * slip + tick_size(stop)
        budget = self._risk_budget()
        if cfg.get("cushion_pct") and self.day["realized"] < self.day["start_equity"] * cfg["cushion_pct"] / 100:
            budget *= cfg.get("cushion_start_fraction", 0.25)
        if cfg.get("aplus_rvol") and w["rvol"] >= cfg["aplus_rvol"]:
            # Breitstein: size up on the best setups (still capped at 2% of equity)
            budget = min(budget * cfg.get("aplus_risk_multiplier", 1.0), self.day["start_equity"] * 0.02)
        qty = int(max(0.0, budget - 2 * self.fee()) / cost_ps)
        max_value = min(self.day["start_equity"] * cfg["max_position_pct"] / 100, self.s["cash"] - self.fee())
        qty = min(qty, int(max_value / price))
        if qty * price < cfg["min_trade_value"]:
            self.log(bar["t"], f"Skipped {w['sym']}: not enough cash or risk room for a sensible size.")
            w["skipped"] = True
            return
        cost = qty * price + self.fee()
        self.s["cash"] = round(self.s["cash"] - cost, 2)
        pos = {
            "sym": w["sym"], "qty": qty, "entry": price, "entry_time": bar["t"].isoformat(),
            "stop": stop, "initial_stop": stop, "risk_ps": round(risk_ps, 4), "high": bar["h"],
            "last": bar["c"], "fees": self.fee(), "rvol": w["rvol"], "atr": w["atr"], "stop_note": "initial",
            "gap_pct": w.get("gap_pct"), "stop_pct": cand["stop_pct"], "market_pct": cand["market_pct"],
            "news_cat": (w.get("news") or {}).get("category"), "news_headline": (w.get("news") or {}).get("headline"),
        }
        self.s["positions"].append(pos)
        self.day["entered"].append(w["sym"])
        self.log(bar["t"], f"BUY {qty} {w['sym']} @ ${price:.3f}, stop ${stop:.3f} (closed above ${w['or_high']:.3f})"
                 if cfg.get("entry_confirm_close") else
                 f"BUY {qty} {w['sym']} @ ${price:.3f}, stop ${stop:.3f} (breakout above ${w['or_high']:.3f})")
        if not cfg.get("entry_confirm_close") and bar["c"] <= stop:
            self._exit(pos, stop, bar["t"], "Stopped out on entry bar")

    def _exit(self, pos: dict, raw_price: float, t, reason: str):
        price = round_tick(raw_price * (1 - self.cfg["slippage_pct"] / 100), "down")
        proceeds = pos["qty"] * price - self.fee()
        self.s["cash"] = round(self.s["cash"] + proceeds, 2)
        fees = pos["fees"] + self.fee()
        gross = pos["qty"] * (price - pos["entry"])
        net = gross - fees
        r_mult = (price - pos["entry"]) / pos["risk_ps"] if pos["risk_ps"] else 0
        self.s["positions"].remove(pos)
        self.day["realized"] = round(self.day["realized"] + net, 2)
        self.day["losses_in_row"] = self.day["losses_in_row"] + 1 if net < 0 else 0
        trade = {
            "id": len(self.trades) + 1,
            "date": self.day["date"],
            "sym": pos["sym"],
            "qty": pos["qty"],
            "entry_time": pos["entry_time"],
            "entry_price": pos["entry"],
            "exit_time": pd.Timestamp(t).isoformat(),
            "exit_price": price,
            "initial_stop": pos["initial_stop"],
            "fees": round(fees, 2),
            "gross": round(gross, 2),
            "net": round(net, 2),
            "pct": round(net / (pos["qty"] * pos["entry"]) * 100, 2),
            "r": round(r_mult, 2),
            "rvol": pos.get("rvol"),
            "gap_pct": pos.get("gap_pct"),
            "stop_pct": pos.get("stop_pct"),
            "market_pct": pos.get("market_pct"),
            "news_cat": pos.get("news_cat"),
            "news_headline": pos.get("news_headline"),
            "reason": reason,
        }
        self.trades.append(trade)
        self.log(t, f"SELL {pos['qty']} {pos['sym']} @ ${price:.3f} ({reason}) net ${net:+.2f}")

    def _manage(self, pos: dict, bar: dict):
        cfg = self.cfg
        # 1) stop hit? (gap through stop fills at the open)
        if bar["l"] <= pos["stop"]:
            fill = min(pos["stop"], bar["o"])
            reason = {"initial": "Stop loss", "breakeven": "Breakeven stop", "trail": "Trailing stop"}[pos["stop_note"]]
            self._exit(pos, fill, bar["t"], reason)
            return
        if cfg.get("profit_target_r"):
            target = round_tick(pos["entry"] + cfg["profit_target_r"] * pos["risk_ps"], "up")
            if bar["h"] >= target:
                self._exit(pos, max(target, bar["o"]), bar["t"], f"Profit target {cfg['profit_target_r']}R")
                return
        pos["last"] = bar["c"]
        pos["high"] = max(pos["high"], bar["h"])
        r = pos["risk_ps"]
        vw = self.day.get("tracks", {}).get(pos["sym"], {}).get("vwap")
        if cfg.get("vwap_exit") and vw and bar["c"] < vw:
            self._exit(pos, bar["c"], bar["t"], "Closed below VWAP")
            return
        tsm = cfg.get("time_stop_minutes")
        if tsm and (bar["t"] - pd.Timestamp(pos["entry_time"])).total_seconds() >= tsm * 60 and bar["c"] <= pos["entry"]:
            self._exit(pos, bar["c"], bar["t"], f"Time stop ({tsm} min, not working)")
            return
        # 2) move stop to breakeven once +1R
        if pos["stop_note"] == "initial" and pos["high"] >= pos["entry"] + cfg["breakeven_at_r"] * r:
            pos["stop"] = round_tick(pos["entry"] + tick_size(pos["entry"]), "up")
            pos["stop_note"] = "breakeven"
        # 3) trail once +2R
        if pos["high"] >= pos["entry"] + cfg["trail_start_r"] * r:
            dist = max(cfg["trail_distance_r"] * r, cfg["trail_atr_fraction"] * pos.get("atr", 0))
            trail = round_tick(pos["high"] - dist, "down")
            if trail > pos["stop"]:
                pos["stop"] = trail
                pos["stop_note"] = "trail"

    def process(self, bars_by_sym: dict, unrealized_fn=None):
        """Walk forward through every complete bar not yet processed, in time order."""
        day = self.day
        if day["phase"] != "trading":
            return
        last_ts = pd.Timestamp(day["last_ts"]) if day["last_ts"] else None
        stamps = sorted({b["t"] for bars in bars_by_sym.values() for b in bars
                         if last_ts is None or b["t"] > last_ts})
        index = {sym: {b["t"]: b for b in bars} for sym, bars in bars_by_sym.items()}
        last_entry = hhmm(self.cfg["last_entry_time"])
        exit_t = hhmm(self.cfg["exit_time"])
        or_end = (datetime.combine(datetime.today(), hhmm(OPEN)) +
                  pd.Timedelta(minutes=self.cfg["opening_range_minutes"])).time()

        tracks = day.setdefault("tracks", {})
        for ts in stamps:
            for sym, bars_at in index.items():
                b = bars_at.get(ts)
                if b:
                    tr = tracks.setdefault(sym, {"pv": 0.0, "v": 0.0, "low": b["l"]})
                    tp = (b["h"] + b["l"] + b["c"]) / 3
                    tr["pv"] += tp * b["v"]
                    tr["v"] += b["v"]
                    tr["low"] = min(tr["low"], b["l"])
                    tr["vwap"] = tr["pv"] / tr["v"] if tr["v"] else b["c"]
            if ts.time() < or_end:
                day["last_ts"] = ts.isoformat()
                continue
            # manage open positions
            for pos in list(self.s["positions"]):
                bar = index.get(pos["sym"], {}).get(ts)
                if bar:
                    self._manage(pos, bar)
            # time exit: close everything on the exit bar
            if ts.time() >= exit_t:
                for pos in list(self.s["positions"]):
                    bar = index.get(pos["sym"], {}).get(ts)
                    px = bar["c"] if bar else pos["last"]
                    self._exit(pos, px, ts, "End of day exit")
                day["last_ts"] = ts.isoformat()
                self.close_day(ts)
                return
            unreal = sum(p["qty"] * (p["last"] - p["entry"]) - p["fees"] for p in self.s["positions"])
            self._check_halt(ts, unreal)
            # entries
            if not day["no_new_entries"] and ts.time() < last_entry:
                for w in day["watchlist"]:
                    if len(self.s["positions"]) >= self.cfg["max_positions"]:
                        break
                    if w["sym"] in day["entered"] or w.get("skipped"):
                        continue
                    bar = index.get(w["sym"], {}).get(ts)
                    if bar and self.cfg.get("entry_mode") == "after_or":
                        self._enter(w, bar)
                        if w["sym"] not in day["entered"]:
                            w["skipped"] = True
                        continue
                    if bar and bar["h"] > w["or_high"]:
                        vw = tracks.get(w["sym"], {}).get("vwap")
                        if self.cfg.get("vwap_entry_filter") and vw and bar["c"] < vw:
                            continue  # Breitstein / Zarattini-Aziz: no longs below VWAP
                        self._enter(w, bar)
            day["last_ts"] = ts.isoformat()

    def close_day(self, t=None, prices: dict | None = None):
        """Flatten anything left (e.g. a halted stock) and mark the day finished."""
        for pos in list(self.s["positions"]):
            px = (prices or {}).get(pos["sym"], pos["last"])
            self._exit(pos, px, t or pd.Timestamp.now(tz=TZ), "End of day exit")
        if self.day["phase"] != "closed":
            self.day["phase"] = "closed"
            eq = self.equity()
            self.day["end_equity"] = eq
            hist = self.s.setdefault("daily", [])
            hist[:] = [d for d in hist if d["date"] != self.day["date"]]
            todays = [x for x in self.trades if x["date"] == self.day["date"]]
            hist.append({
                "date": self.day["date"],
                "start_equity": self.day["start_equity"],
                "end_equity": eq,
                "pnl": round(eq - self.day["start_equity"], 2),
                "trades": len(todays),
                "market_change_pct": self.day.get("market_change_pct"),
            })
            self.log(t, f"Day closed. Equity ${eq:,.2f} ({eq - self.day['start_equity']:+.2f}).")
