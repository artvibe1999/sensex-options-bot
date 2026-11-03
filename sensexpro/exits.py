"""Adaptive, thesis-based exit engine (brief section 7, Option A). No fixed-premium-percentage stop.

Components and weights (strategy.yaml exits.weights; brief 7.8):
  structure (30)      a COMPLETED 5-minute close beyond the stored invalidation level = structural exit (immediate);
                      within 0.25 ATR of it = 10-point warning. The level trails confirmed swings (higher lows for
                      CE, lower highs for PE) formed after entry.
  vwap (20)           close on the wrong side of VWAP 10; VWAP slope flat / against 5; failed reclaim/rejection 5.
  momentum (15)       RSI back through 50: 5; MACD histogram against or shrinking 3 bars: 5; EMA9 through EMA20: 3;
                      15-minute ADX falling: 2.
  option_health (15)  option below its day VWAP 4; option EMA9 < EMA21 on 1-minute prices 4; spread > 2x entry
                      spread 4; option not responding to a favourable underlying move 3.
  iv_theta (10)       Kite supplies no IV / Greeks: always MISSING (scored at missing_input_share, never 0).
  time (10)           no meaningful underlying progress after no_progress_min: 5; after no_progress_exit_min with the
                      thesis weakening (VWAP or momentum points): 10.
Profit protection (7.7): once peak profit reaches profit_milestone_premium_pct of the premium paid, the EXIT
threshold drops to milestone_exit_threshold (the runner is kept while the components stay quiet), and a multi-lot
position sells one lot (REDUCE) on the first PREPARE_EXIT. Action mapping: <30 HOLD, <50 REASSESS, <70 PREPARE_EXIT
(EXIT after prepare_bars_to_exit consecutive bars), >=70 EXIT.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

from . import indicators as ind
from .features import Frame
from .models import CE, ExitAssessment, OptionQuote, Position

HOLD, REASSESS, PREPARE, EXIT, REDUCE = "HOLD", "REASSESS", "PREPARE_EXIT", "EXIT", "REDUCE"


@dataclass
class OptionTracker:
    """1-minute option mid prices built from executable quotes (never a single LTP print), plus spreads."""
    closes: list = field(default_factory=list)
    _minute: Optional[dt.datetime] = None
    _last_mid: Optional[float] = None
    last_quote: Optional[OptionQuote] = None

    def update(self, q: Optional[OptionQuote], now: dt.datetime) -> None:
        minute = now.replace(second=0, microsecond=0)
        if self._minute is not None and minute > self._minute and self._last_mid is not None:
            self.closes.append(self._last_mid)               # the previous minute closed
            self.closes = self.closes[-120:]
        self._minute = minute
        if q is not None and q.mid is not None:
            self._last_mid = q.mid
            self.last_quote = q


def update_structure(pos: Position, f: Frame, i: int, trail_bars: int) -> Optional[float]:
    """Trail the invalidation level to the latest confirmed swing (after entry) that tightens it. Returns the new level
    or None. A swing low at k is confirmed when `trail_bars` bars on each side have higher lows (CE)."""
    up = pos.direction == CE
    best = None
    for k in range(i - trail_bars, max(trail_bars - 1, i - 30), -1):
        if k - trail_bars < 0 or f.bars[k].start < pos.entry_time:
            break
        b = f.bars[k]
        side = f.bars[k - trail_bars:k] + f.bars[k + 1:k + 1 + trail_bars]
        if len(side) < 2 * trail_bars:
            continue
        if up and all(b.low < x.low for x in side) and b.low > pos.invalidation:
            best = b.low
            break
        if not up and all(b.high > x.high for x in side) and b.high < pos.invalidation:
            best = b.high
            break
    return best


class AdaptiveExitEngine:
    def __init__(self, p: SimpleNamespace, regime_p: SimpleNamespace) -> None:
        self.p, self.rp = p, regime_p
        self.w = dict(vars(p.weights))

    def _missing(self, name: str) -> float:
        return self.w[name] * float(self.p.missing_input_share)

    def assess(self, pos: Position, f: Optional[Frame], i: Optional[int], opt: OptionTracker, spot: Optional[float],
               now: dt.datetime, new_bar: bool) -> ExitAssessment:
        p, w = self.p, self.w
        up = pos.direction == CE
        comp: dict[str, float] = {}
        missing: list[str] = []
        why: list[str] = []
        structural = False
        bar_ok = f is not None and i is not None and i >= 1 and f.atr[i] is not None
        # ---------------- A structure
        if not bar_ok:
            comp["structure"] = self._missing("structure")
            missing.append("structure")
        else:
            b, atr = f.bars[i], f.atr[i]
            broke = b.close < pos.invalidation if up else b.close > pos.invalidation
            if broke and b.end > pos.entry_time:
                comp["structure"] = float(w["structure"])
                structural = p.structure_confirm == "close"
                why.append(f"completed close {b.close:.2f} {'below' if up else 'above'} invalidation "
                           f"{pos.invalidation:.2f}")
            elif abs(b.close - pos.invalidation) <= 0.25 * atr:
                comp["structure"] = 10.0
                why.append("close within 0.25 ATR of the invalidation level")
            else:
                comp["structure"] = 0.0
        # ---------------- B VWAP
        if not bar_ok or f.vwap[i] is None:
            comp["vwap"] = self._missing("vwap")
            missing.append("vwap")
        else:
            b, v = f.bars[i], f.vwap[i]
            pts = 0.0
            if (up and b.close < v) or (not up and b.close > v):
                pts += 10
                why.append("lost VWAP" if up else "reclaimed VWAP")
            s = f.vwap_slope(i)
            if s is None or (s / f.atr[i]) * (1 if up else -1) < self.rp.vwap_slope_atr:
                pts += 5
            pb, pv = f.bars[i - 1], f.vwap[i - 1]
            if pv is not None and ((up and pb.close < pv and b.high > v and b.close < v) or
                                   (not up and pb.close > pv and b.low < v and b.close > v)):
                pts += 5
                why.append("failed VWAP retest")
            comp["vwap"] = min(pts, float(w["vwap"]))
        # ---------------- C momentum
        if not bar_ok or f.rsi[i] is None or f.macd_hist[i] is None:
            comp["momentum"] = self._missing("momentum")
            missing.append("momentum")
        else:
            pts = 0.0
            r = f.rsi[i]
            if (up and r < 50) or (not up and r > 50):
                pts += 5
            h = f.macd_hist
            against = (h[i] < 0) if up else (h[i] > 0)
            shrinking = i >= 3 and None not in (h[i - 3], h[i - 2], h[i - 1]) and \
                (all(abs(h[k]) < abs(h[k - 1]) for k in (i - 2, i - 1, i)))
            if against or shrinking:
                pts += 5
            if f.ema_f[i] is not None and f.ema_s[i] is not None and \
                    ((up and f.ema_f[i] < f.ema_s[i]) or (not up and f.ema_f[i] > f.ema_s[i])):
                pts += 3
            j = f.j15(i)
            if j is not None and j >= 2 and None not in (f.adx15[j], f.adx15[j - 2]) and f.adx15[j] < f.adx15[j - 2]:
                pts += 2
            comp["momentum"] = min(pts, float(w["momentum"]))
            if pts >= 10:
                why.append("momentum deteriorating")
        # ---------------- D option health
        q = opt.last_quote
        if q is None or q.mid is None:
            comp["option_health"] = self._missing("option_health")
            missing.append("option_health")
        else:
            pts, miss = 0.0, 0
            if q.avg_price:
                pts += 4 if q.mid < q.avg_price else 0
            else:
                miss += 4
            ef, es = (ind.ema(opt.closes, p.option_ema[0]), ind.ema(opt.closes, p.option_ema[1])) if opt.closes \
                else ([], [])
            if ef and ef[-1] is not None and es[-1] is not None:
                pts += 4 if ef[-1] < es[-1] else 0
            else:
                miss += 4
            sp = q.spread_pct
            if sp is not None and pos.entry_spread_pct > 0 and sp > 2 * max(pos.entry_spread_pct, 0.2):
                pts += 4
                why.append(f"option spread widened to {sp:.2f}%")
            if bar_ok and spot is not None:
                move = (spot - pos.entry_spot) * (1 if up else -1)
                if move >= p.progress_atr * f.atr[i] and q.mid <= pos.entry_price:
                    pts += 3
                    why.append("option not responding to the underlying move")
            comp["option_health"] = min(pts + miss * float(p.missing_input_share), float(w["option_health"]))
            if miss:
                missing.append("option_health(partial)")
        # ---------------- E IV / theta: no reliable data from Kite
        comp["iv_theta"] = self._missing("iv_theta")
        missing.append("iv_theta")
        # ---------------- time / opportunity cost
        mins = (now - pos.entry_time).total_seconds() / 60
        if not bar_ok:
            comp["time"] = self._missing("time")
            missing.append("time")
        else:
            progressed = pos.peak_spot_move >= p.progress_atr * f.atr[i]
            pts = 0.0
            if mins >= p.no_progress_min and not progressed:
                pts = 5.0
                if mins >= p.no_progress_exit_min and (comp.get("vwap", 0) > 0 or comp.get("momentum", 0) >= 5):
                    pts = 10.0
                why.append(f"no meaningful progress after {mins:.0f} min")
            comp["time"] = pts
        total = round(sum(comp.values()), 1)
        # ---------------- action mapping (+ profit protection)
        multi_lot = pos.qty > pos.contract.lot_size
        if structural or total >= p.prepare_below:
            action = EXIT
        elif pos.milestone_hit and total >= p.milestone_exit_threshold:
            # profit protection: weakening after the milestone. A multi-lot position sells one lot per completed bar
            # (whole lots only); a single lot, or reduce disabled, exits.
            if multi_lot and p.reduce_lots_on_weakening:
                action = REDUCE if new_bar else PREPARE
            else:
                action = EXIT
            why.append("weakening after the profit milestone")
        elif total >= p.reassess_below:
            action = PREPARE
        elif total >= p.hold_below:
            action = REASSESS
        else:
            action = HOLD
        if action == PREPARE and new_bar:
            pos.prepare_count += 1
        elif action in (HOLD, REASSESS) and new_bar:
            pos.prepare_count = 0
        if action == PREPARE and pos.prepare_count >= p.prepare_bars_to_exit:
            action = EXIT
            why.append(f"PREPARE_EXIT on {pos.prepare_count} consecutive bars")
        return ExitAssessment(now, comp, tuple(missing), total, action, tuple(why), structural)
