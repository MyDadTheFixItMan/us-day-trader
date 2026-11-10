"""End-of-day review: learn from losses without over-fitting to a few bad days.

Three layers:
1. Lessons: every losing trade gets a plain-English diagnosis shown on the dashboard.
2. Risk throttle: trade smaller after losing days, step back up after winning days.
3. Learned filters: once a type of trade (e.g. "gap over 5%") has enough history and
   clearly loses money, the bot stops taking it. Filters expire so they get re-tested.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from engine import tick_size

FEATURES = {
    "price": ("Share price", [(0, 1, "under $1"), (1, 5, "$1–$5"), (5, 20, "$5–$20"), (20, 1e9, "over $20")]),
    "gap": ("Opening gap", [(-1e9, 0, "gapped down"), (0, 2, "gap 0–2%"), (2, 5, "gap 2–5%"), (5, 1e9, "gap over 5%")]),
    "rvol": ("Relative volume", [(0, 3, "2–3× volume"), (3, 5, "3–5× volume"), (5, 10, "5–10× volume"), (10, 1e9, "over 10× volume")]),
    "entry": ("Entry time", [(0, 600, "before 10:00"), (600, 630, "10:00–10:30"), (630, 1e9, "after 10:30")]),
    "stop": ("Stop distance", [(0, 1, "stop under 1%"), (1, 2, "stop 1–2%"), (2, 1e9, "stop over 2%")]),
    "market": ("Market at open", [(-1e9, 0, "market down"), (0, 1e9, "market up")]),
    "news": ("Overnight news", None),
}


def features(t: dict) -> dict:
    """Bucket a trade (or a would-be trade) by the features above."""
    hm = t.get("entry_time", "T09:45")[11:16] or "09:45"
    raw = {
        "price": t.get("entry_price"),
        "gap": t.get("gap_pct"),
        "rvol": t.get("rvol"),
        "entry": int(hm[:2]) * 60 + int(hm[3:5]),
        "stop": t.get("stop_pct"),
        "market": t.get("market_pct"),
    }
    out = {"news": f"news: {t['news_cat']}" if t.get("news_cat") else "no news"} if "id" in t or "news_cat" in t else {}
    for k, v in raw.items():
        if v is None:
            continue
        for lo, hi, label in FEATURES[k][1]:
            if lo <= v < hi:
                out[k] = label
                break
    return out


def diagnose(t: dict, cfg: dict, start_equity: float) -> list[str]:
    """Plain-English reasons a losing trade went wrong."""
    out = []
    budget = start_equity * cfg["risk_per_trade_pct"] / 100
    if t["entry_time"][:16] == t["exit_time"][:16]:
        out.append("False breakout: it fell back through the stop in the same 5 minutes it was bought.")
    if -t["net"] > budget * 1.1:
        out.append(f"Lost ${-t['net']:.0f}, more than the planned ${budget:.0f}: price stepped past the stop and fees added up.")
    step_pct = tick_size(t["entry_price"]) / t["entry_price"] * 100
    if step_pct > 0.5:
        out.append(f"Low-priced share: one price step is {step_pct:.1f}% of the price, so slippage hurts.")
    hm = t["entry_time"][11:16]
    if hm >= "10:30":
        out.append("Late entry: breakouts after 10:30am tend to have less follow-through.")
    if (t.get("market_pct") or 0) < 0:
        out.append("The S&P 500 was down at the open: breakouts struggle when the market is weak.")
    if (t.get("gap_pct") or 0) > 5:
        out.append("Big opening gap: much of the move had already happened before the breakout.")
    if not out:
        out.append("A normal stop-out: the breakout didn't follow through. Small, planned losses like this are part of the strategy.")
    return out


def stats_by_bucket(trades: list) -> dict:
    agg = defaultdict(lambda: {"n": 0, "wins": 0, "r": 0.0, "net": 0.0})
    for t in trades:
        for k, label in features(t).items():
            a = agg[(k, label)]
            a["n"] += 1
            a["wins"] += t["net"] > 0
            a["r"] += t["r"]
            a["net"] += t["net"]
    return agg


def review_day(state: dict, trades: list, cfg: dict):
    """Called once after the day closes."""
    day = state["day"]
    today = [t for t in trades if t["date"] == day["date"]]
    L = state.setdefault("learning", {"risk_multiplier": 1.0, "blocks": [], "lessons": [], "changes": []})

    # 1) Lessons from today's losses
    for t in today:
        if t["net"] < 0 and not any(x["trade_id"] == t["id"] for x in L["lessons"]):
            L["lessons"].append({"trade_id": t["id"], "date": t["date"], "sym": t["sym"], "net": t["net"],
                                 "notes": diagnose(t, cfg, day["start_equity"])})
    L["lessons"] = L["lessons"][-60:]

    # 2) Risk throttle (trade smaller when cold, back up when warm)
    pnl = round(day.get("end_equity", day["start_equity"]) - day["start_equity"], 2)
    old = L["risk_multiplier"]
    if today:
        if pnl < 0:
            L["risk_multiplier"] = max(cfg["learn_min_risk_multiplier"], round(old * 0.75, 3))
        elif pnl > 0:
            L["risk_multiplier"] = min(1.0, round(old + 0.25, 3))
    if L["risk_multiplier"] != old:
        word = "Cut" if L["risk_multiplier"] < old else "Raised"
        L["changes"].append({"date": day["date"], "text":
            f"{word} risk per trade to {cfg['risk_per_trade_pct'] * L['risk_multiplier']:.2f}% of equity after a "
            f"{'losing' if pnl < 0 else 'winning'} day ({pnl:+.2f})."})

    # 3) Learned filters: block a trade type only with enough evidence
    d = date.fromisoformat(day["date"])
    L["blocks"] = [b for b in L["blocks"] if (d - date.fromisoformat(b["since"])).days < cfg["learn_block_days"]]
    overall_r = sum(t["r"] for t in trades) / len(trades) if trades else 0
    for (k, label), a in stats_by_bucket(trades).items():
        if a["n"] < cfg["learn_min_trades"]:
            continue
        avg_r = a["r"] / a["n"]
        bad = avg_r <= cfg["learn_block_avg_r"] and avg_r < overall_r - 0.25
        blocked = any(b["feature"] == k and b["label"] == label for b in L["blocks"])
        if bad and not blocked:
            L["blocks"].append({"feature": k, "label": label, "since": day["date"], "n": a["n"],
                                "avg_r": round(avg_r, 2), "win_rate": round(a["wins"] / a["n"] * 100)})
            L["changes"].append({"date": day["date"], "text":
                f"Stopped taking trades with {label}: {a['n']} trades averaged {avg_r:+.2f}R "
                f"({a['wins'] / a['n'] * 100:.0f}% winners). Re-tested after {cfg['learn_block_days']} days."})
    L["changes"] = L["changes"][-40:]

    # Scoreboard for the dashboard
    L["buckets"] = sorted(
        [{"feature": FEATURES[k][0], "label": label, "n": a["n"], "win_rate": round(a["wins"] / a["n"] * 100),
          "avg_r": round(a["r"] / a["n"], 2), "net": round(a["net"], 2)}
         for (k, label), a in stats_by_bucket(trades).items()],
        key=lambda b: (b["feature"], b["label"]))
    day["reviewed"] = True


def blocked_reason(state: dict, candidate: dict) -> str | None:
    L = state.get("learning") or {}
    f = features(candidate)
    for b in L.get("blocks", []):
        if f.get(b["feature"]) == b["label"]:
            return f"learned rule: {b['label']} averaged {b['avg_r']:+.2f}R over {b['n']} trades"
    return None
