"""Research metrics (brief section 9) and chronological walk-forward splits.

Splits (fixed, chronological, no parameter is tuned on any of them: the brief's defaults are evaluated as they are):
    in-sample reference 2018-2021 | validation 2022-2023 | test 2024 onward.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Iterable

SPLITS = (("IS_2018_2021", "2018", "2022"), ("VAL_2022_2023", "2022", "2024"), ("TEST_2024_on", "2024", "9999"))


def _stats(ts: list[dict]) -> dict:
    n = len(ts)
    if not n:
        return {"n": 0}
    pts = [t["points"] for t in ts]
    wins = [p for p in pts if p > 0]
    losses = [p for p in pts if p <= 0]
    cum, peak, dd = 0.0, 0.0, 0.0
    streak = worst_streak = 0
    for p in pts:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        streak = streak + 1 if p <= 0 else 0
        worst_streak = max(worst_streak, streak)
    model = [t["model"]["net_rs"] for t in ts if t.get("model")]
    days = {t["date"] for t in ts}
    months: dict[str, float] = defaultdict(float)
    for t in ts:
        months[t["date"][:7]] += t["model"]["net_rs"] if t.get("model") else 0.0
    return {
        "n": n, "days": len(days), "win_rate": round(100 * len(wins) / n, 1),
        "exp_pts": round(sum(pts) / n, 2), "avg_win_pts": round(statistics.mean(wins), 2) if wins else 0.0,
        "avg_loss_pts": round(statistics.mean(losses), 2) if losses else 0.0,
        "pf": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "max_dd_pts": round(dd, 1), "largest_loss_pts": round(min(pts), 1),
        "p05_pts": round(sorted(pts)[max(0, int(0.05 * n) - 1)], 1), "worst_loss_streak": worst_streak,
        "avg_mfe": round(statistics.mean(t["mfe"] for t in ts), 1),
        "avg_mae": round(statistics.mean(t["mae"] for t in ts), 1),
        "avg_hold_min": round(statistics.mean(t["hold_min"] for t in ts)),
        "model_net_rs": round(sum(model), 0) if model else None,
        "model_exp_rs": round(sum(model) / len(model), 1) if model else None,
        "model_months_pos_pct": round(100 * sum(1 for v in months.values() if v > 0) / len(months), 1)
        if months else None,
    }


def _by(ts: Iterable[dict], key) -> dict:
    g: dict = defaultdict(list)
    for t in ts:
        g[key(t)].append(t)
    return {k: _stats(v) for k, v in sorted(g.items())}


def _tod(t: dict) -> str:
    h, m = map(int, t["entry"].split(":"))
    mins = h * 60 + m
    return "09:30-10:30" if mins < 630 else "10:30-12:00" if mins < 720 else "12:00-13:30" if mins < 810 else "13:30+"


def _vixb(t: dict) -> str:
    v = t.get("vix")
    return "unknown" if v is None else "<13" if v < 13 else "13-18" if v < 18 else ">=18"


def summarize(trades: list[dict], signals: list[dict]) -> dict:
    sig_fwd = defaultdict(list)
    for s in signals:
        if "30m" in s:
            sig_fwd[s["strategy"]].append(s["30m"])
    return {
        "all": _stats(trades),
        "splits": {name: _stats([t for t in trades if lo <= t["date"] < hi]) for name, lo, hi in SPLITS},
        "by_year": _by(trades, lambda t: t["date"][:4]),
        "by_strategy": _by(trades, lambda t: t["strategy"]),
        "by_regime": _by(trades, lambda t: t["regime"]),
        "by_direction": _by(trades, lambda t: t["direction"]),
        "by_time": _by(trades, _tod),
        "by_vix": _by(trades, _vixb),
        "by_exit": _by(trades, lambda t: t["reason"].split("_")[0]),
        "signal_30m_fwd_pts": {k: {"n": len(v), "mean": round(statistics.mean(v), 2),
                                   "hit": round(100 * sum(1 for x in v if x > 0) / len(v), 1)}
                               for k, v in sig_fwd.items() if v},
    }
