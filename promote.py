"""Promote variants to live: backtest approval AND forward (shadow) evidence.

  python promote.py                      # defaults: >=20 forward trades, PF >= 1.2, net > 0
  python promote.py --min-trades 30 --min-pf 1.3

OI/gamma-rule variants (ids ending in |oi=<rule>) cannot be backtested (no historical OI). They get a
STRICTER forward-only test: more trades, higher profit factor, AND they must beat their plain twin
(same trade without the filter) on both average net per trade and profit factor over the same period.

Reads  data/approved_backtest.json  (from backtest.py)
       data/sxbot.db                (shadow_trades recorded by the bot every day)
Writes data/approved_live.json      (the ONLY file --live trades from; valid 30 days)

Re-run at least weekly. If you do not, the approvals expire and the live bot
stops opening new trades (fail closed).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3

from config import IST, TIER_CAPITAL, RiskParams, Schedule
from strategy_library import build_library


def _pf(nets: list[float]) -> float:
    wins, losses = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
    return 99.0 if losses == 0 and wins > 0 else (wins / losses if losses else 0.0)


def _tstat(nets: list[float]) -> float:
    """t-statistic of the mean net P&L per trade (how many standard errors above zero the edge is)."""
    n = len(nets)
    if n < 2:
        return 0.0
    mean = sum(nets) / n
    var = sum((x - mean) ** 2 for x in nets) / (n - 1)
    if var <= 0:
        return 99.0 if mean > 0 else 0.0
    return mean / (var / n) ** 0.5


def _explore_trades(db, since: str) -> list[tuple[str, str, float, float, str]]:
    """Closed paper exploration trades: (variant, day, net per ONE lot, max loss per lot, scanner setup)."""
    out = []
    try:
        rows = db.execute("SELECT variant, ts, net, payload FROM trades WHERE event='exit' AND mode='PAPER' AND ts >= ?",
                          (since,)).fetchall()
    except sqlite3.Error:
        return out
    for vid, ts, net, payload in rows:
        try:
            pl = json.loads(payload or "{}")
        except ValueError:
            continue
        if not pl.get("explore"):
            continue
        lots = max(int(pl.get("lots") or 1), 1)
        out.append((vid, ts[:10], net / lots, float(pl.get("max_loss_per_lot") or 0.0), str(pl.get("setup") or "?")))
    return out


def _tier_nets(rows: list[tuple[float, float]], capital: float, spot_rules: bool = False) -> list[float]:
    """Forward one-lot results (net, max_loss) re-expressed for an account size: n lots from the real sizing rule,
    trades that do not fit are skipped. Costs are scaled with n too, which slightly OVERstates them (conservative)."""
    from types import SimpleNamespace
    from risk_manager import RiskManager
    rm = RiskManager(RiskParams(), Schedule())
    rm.apply_capital(capital, "auto")
    out = []
    for net, ml in rows:
        n = rm.size(SimpleNamespace(max_loss_per_lot=ml, spot_rules=spot_rules), capital) if ml and ml > 0 else 0
        if n >= 1:
            out.append(net * n)
    return out


def main() -> int:
    data_dir = os.getenv("DATA_DIR", "data")
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=data_dir)
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--min-pf", type=float, default=1.2)
    ap.add_argument("--min-trades-oi", type=int, default=40, help="forward trades needed by OI-rule variants")
    ap.add_argument("--min-pf-oi", type=float, default=1.3)
    ap.add_argument("--min-explore", type=int, default=10,
                    help="scanner-selected paper trades a non-backtested variant needs (and they must be net positive)")
    ap.add_argument("--min-t", type=float, default=1.0,
                    help="mean net per trade must be this many standard errors above zero (guards against luck: "
                         "with ~1000 variants some always look good on 20 trades). 0 disables.")
    ap.add_argument("--lookback-days", type=int, default=90, help="forward window considered")
    ap.add_argument("--dd-capital-mult", type=float, default=1.5,
                    help="a tier is promoted only if its capital >= this x the backtest max drawdown per lot")
    ap.add_argument("--since", help="only forward evidence entered on/after this date (YYYY-MM-DD), e.g. the day the "
                                    "shadow book started using the live exit rules; overrides --lookback-days")
    ap.add_argument("--valid-days", type=int, default=30)
    args = ap.parse_args()

    try:
        with open(os.path.join(args.data_dir, "approved_backtest.json"), encoding="utf-8") as f:
            bt = json.load(f)
    except OSError:
        print("No approved_backtest.json: only OI-rule variants can be promoted.")
        bt = {"variants": []}
    try:                                         # intraday signal variants (intraday_backtest.py)
        with open(os.path.join(args.data_dir, "approved_intraday.json"), encoding="utf-8") as f:
            bt["variants"] = bt["variants"] + json.load(f)["variants"]
    except (OSError, ValueError, KeyError):
        pass
    since = (dt.date.fromisoformat(args.since).isoformat() if args.since
             else (dt.date.today() - dt.timedelta(days=args.lookback_days)).isoformat())
    db = sqlite3.connect(os.path.join(args.data_dir, "sxbot.db"))
    fwd: dict[str, list[float]] = {}
    fwd_rows: dict[str, list[tuple[float, float]]] = {}
    shadow_days: set[tuple[str, str]] = set()
    for vid, net, ml, ets in db.execute("SELECT variant, net, max_loss, entry_ts FROM shadow_trades WHERE entry_ts >= ?",
                                        (since,)):
        fwd.setdefault(vid, []).append(net)
        fwd_rows.setdefault(vid, []).append((net, ml))
        shadow_days.add((vid, ets[:10]))
    # Paper "[EXPLORE ...]" trades (real paper executions). A variant already shadow-traded the same day is NOT
    # counted again (it would be the same trade twice); explore adds only days the shadow book did not trade.
    explore = _explore_trades(db, since)
    explore_n: dict[str, int] = {}
    explore_net: dict[str, float] = {}
    for vid, day, net1, ml, _setup in explore:
        if (vid, day) in shadow_days:
            continue
        fwd.setdefault(vid, []).append(net1)
        fwd_rows.setdefault(vid, []).append((net1, ml))
        explore_n[vid] = explore_n.get(vid, 0) + 1
        explore_net[vid] = explore_net.get(vid, 0.0) + net1
    db.close()

    def fwd_tiers(vid: str, min_n: int, min_pf: float) -> list[str]:
        out = []
        for tier, cap in TIER_CAPITAL.items():
            nets = _tier_nets(fwd_rows.get(vid, []), cap, vid.startswith("ib_"))
            if len(nets) >= min_n and sum(nets) > 0 and _pf(nets) >= min_pf and _tstat(nets) >= args.min_t:
                out.append(tier)
        return out

    live, rows = [], []
    for v in bt["variants"]:
        nets = fwd.get(v["id"], [])
        n, total, pf = len(nets), sum(nets), _pf(nets)
        ok = n >= args.min_trades and total > 0 and pf >= args.min_pf and _tstat(nets) >= args.min_t
        rows.append((v["id"], n, total, pf, ok))
        if ok:
            ft = fwd_tiers(v["id"], args.min_trades, args.min_pf)
            dd = float((v.get("full") or {}).get("max_dd") or 0.0)
            # a lot cannot be split: only promote for an account that can survive the backtest's worst drawdown
            tiers = [t for t in ft if ("tiers" not in v or t in v["tiers"])
                     and TIER_CAPITAL[t] >= args.dd_capital_mult * dd]
            if tiers:
                live.append({"id": v["id"], "score": v["score"], "tiers": tiers,
                             "forward": {"n": n, "net": total, "pf": pf}})

    oi_rows = []
    lib_ids = {s.id for s in build_library()}
    for spec in build_library():
        if not spec.forward_only:
            continue
        nets, base = fwd.get(spec.id, []), fwd.get(spec.base_id, [])
        n, total, pf = len(nets), sum(nets), _pf(nets)
        bn = len(base)
        avg, bavg, bpf = (total / n if n else 0.0), (sum(base) / bn if bn else 0.0), (_pf(base) if bn else 0.0)
        # No evidence from the plain twin must never pass by default: if a twin exists in the library, wait for it;
        # only variants that have no twin at all rely on the stricter absolute forward bar.
        twin_ok = (avg > bavg and pf > bpf) if bn >= 20 else (spec.base_id not in lib_ids and spec.intraday_trend)
        ok = (n >= args.min_trades_oi and total > 0 and pf >= args.min_pf_oi and twin_ok
              and _tstat(nets) >= args.min_t)
        oi_rows.append((spec.id, n, total, pf, bn, bavg, ok))
        tiers = fwd_tiers(spec.id, args.min_trades_oi, args.min_pf_oi) if ok else []
        if ok and tiers:
            live.append({"id": spec.id, "score": pf, "evidence": "forward_only", "tiers": tiers,
                         "forward": {"n": n, "net": total, "pf": pf},
                         "plain_twin": {"n": bn, "avg": bavg, "pf": bpf}})

    # Variants with no backtest approval that earn promotion on forward evidence alone (same strict bar as the
    # OI-rule variants), but only if their own scanner-selected paper trades are also positive.
    known = {v["id"] for v in bt["variants"]} | {s.id for s in build_library() if s.forward_only}
    explore_rows = []
    for vid in sorted(explore_n):
        if vid in known:
            continue
        nets = fwd.get(vid, [])
        n, total, pf, tt = len(nets), sum(nets), _pf(nets), _tstat(nets)
        ok = (explore_n[vid] >= args.min_explore and explore_net[vid] > 0 and n >= args.min_trades_oi
              and total > 0 and pf >= args.min_pf_oi and tt >= args.min_t)
        explore_rows.append((vid, n, total, pf, explore_n[vid], explore_net[vid], ok))
        tiers = fwd_tiers(vid, args.min_trades_oi, args.min_pf_oi) if ok else []
        if ok and tiers:
            live.append({"id": vid, "score": pf, "evidence": "forward_explore", "tiers": tiers,
                         "forward": {"n": n, "net": total, "pf": pf},
                         "explore": {"n": explore_n[vid], "net": explore_net[vid]}})

    print(f"{'variant':<64} {'fwd n':>6} {'fwd net':>10} {'pf':>6}  live")
    for vid, n, total, pf, ok in sorted(rows, key=lambda r: -r[2])[:25]:
        print(f"{vid:<64} {n:>6} {total:>10,.0f} {pf:>6.2f}  {'YES' if ok else '-'}")
    if any(r[1] for r in oi_rows):
        print("\nOI/gamma-rule variants (forward-only; must beat the plain twin):")
        print(f"{'variant':<72} {'n':>4} {'net':>9} {'pf':>5} {'twin n':>6} {'twin avg':>9}  live")
        for vid, n, total, pf, bn, bavg, ok in sorted(oi_rows, key=lambda r: -r[2])[:15]:
            if n:
                print(f"{vid:<72} {n:>4} {total:>9,.0f} {pf:>5.2f} {bn:>6} {bavg:>9,.0f}  {'YES' if ok else '-'}")
    if explore:
        print("\nPaper exploration results by scanner setup (net per ONE lot, after costs):")
        by: dict[str, list[float]] = {}
        for _v, _d, net1, _m, setup in explore:
            by.setdefault(setup, []).append(net1)
        for setup, nets in sorted(by.items(), key=lambda kv: -sum(kv[1])):
            print(f"  {setup:<20} n={len(nets):<4} net={sum(nets):>9,.0f}  pf={_pf(nets):.2f}  win%={100 * sum(x > 0 for x in nets) / len(nets):.0f}")
    if explore_rows:
        print("\nExploration-only candidates (no backtest approval; strict forward bar):")
        for vid, n, total, pf, en, enet, ok in sorted(explore_rows, key=lambda r: -r[2])[:15]:
            print(f"  {vid:<64} n={n:<4} net={total:>8,.0f} pf={pf:.2f} explore n={en} net={enet:>8,.0f}  {'YES' if ok else '-'}")
    doc = {"generated_at": dt.datetime.now(IST).isoformat(), "source": "promote",
           "valid_until": str(dt.date.today() + dt.timedelta(days=args.valid_days)), "variants": live}
    path = os.path.join(args.data_dir, "approved_live.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"\n{len(live)} variants promoted -> {path}"
          + ("" if live else "  (none qualified: the live bot will not trade; keep paper trading)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
