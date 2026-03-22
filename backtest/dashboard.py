"""Interactive dashboard with metrics overview + clickable trade drill-downs."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import numpy as np
from backtest.engine import BacktestResult, BacktestTrade
from backtest.analyzer import PerformanceMetrics


def _generate_narrative(trade: BacktestTrade) -> str:
    ctx = trade.context
    if not ctx:
        return "No detailed context available for this trade."

    trend = ctx.get("trend", {})
    details = trend.get("details", {})
    signal = ctx.get("signal", {})
    atr_val = ctx.get("atr_value", 0)
    direction = "BULLISH" if trend.get("direction", 0) == 1 else "BEARISH"
    strength = trend.get("strength", 0)
    opt_type = "Call (CE)" if signal.get("instrument_type") == "CE" else "Put (PE)"
    rsi_val = trend.get("rsi", 0)
    bull_indicators = [k for k, v in details.items() if v == "BULL"]
    bear_indicators = [k for k, v in details.items() if v == "BEAR"]
    agreeing = bull_indicators if direction == "BULLISH" else bear_indicators
    opposing = bear_indicators if direction == "BULLISH" else bull_indicators
    ind_names = {"ema_cross": "EMA 9/21 crossover", "price_vs_ema50": "price vs EMA 50",
                 "rsi": f"RSI ({rsi_val:.0f})", "macd": "MACD histogram",
                 "supertrend": "Supertrend", "price_action": "recent price action"}
    agreeing_text = ", ".join(ind_names.get(i, i) for i in agreeing)
    opposing_text = ", ".join(ind_names.get(i, i) for i in opposing) if opposing else "none"
    risk = abs(trade.entry_price - trade.stop_loss)
    reward = abs(trade.target - trade.entry_price)
    rr = reward / risk if risk > 0 else 0

    lines = [
        f"This {trade.strategy} trade was initiated at {trade.entry_time.strftime('%Y-%m-%d %H:%M')} "
        f"based on a {direction} trend signal with {strength:.0f}% indicator agreement. "
        f"A {opt_type} option ({trade.tradingsymbol}) was selected at strike {signal.get('strike', 'N/A')} "
        f"with a premium of Rs.{trade.entry_price:.2f}.",
        f"The trend was confirmed by {len(agreeing)} indicators: {agreeing_text}. "
        f"Opposing signals came from: {opposing_text}.",
    ]
    ema9_val = trend.get("ema9", 0)
    ema21_val = trend.get("ema21", 0)
    if ema9_val and ema21_val:
        lines.append(
            f"The EMA 9 ({ema9_val:.2f}) was {'above' if ema9_val > ema21_val else 'below'} "
            f"EMA 21 ({ema21_val:.2f}), confirming short-term {'bullish' if ema9_val > ema21_val else 'bearish'} momentum.")
    lines.append(
        f"RSI was at {rsi_val:.1f}, indicating "
        f"{'overbought conditions' if rsi_val > 70 else 'oversold conditions' if rsi_val < 30 else 'neutral momentum'}. "
        f"The ATR was {atr_val:.2f} points, used to calculate a dynamic stop-loss.")
    lines.append(
        f"The option had OI of {signal.get('oi', 0):,} and volume of {signal.get('volume', 0):,}. "
        f"It scored {trade.score:.0f}/100 in the ranking system.")
    lines.append(
        f"SL at Rs.{trade.stop_loss:.2f} (Rs.{risk:.2f} risk/unit), target at Rs.{trade.target:.2f} "
        f"(Rs.{reward:.2f} reward), R:R 1:{rr:.2f}. "
        f"Position: {trade.original_qty} units ({trade.original_qty // trade.lot_size} lot).")
    if trade.exit_price > 0:
        pnl_word = "profit" if trade.net_pnl > 0 else "loss"
        lines.append(
            f"Exited at Rs.{trade.exit_price:.2f} ({trade.exit_reason.replace('_', ' ').lower()}), "
            f"net {pnl_word} Rs.{trade.net_pnl:+,.2f} after Rs.{trade.charges:.2f} charges. "
            f"MFE: Rs.{trade.max_favorable:+,.2f}, MAE: Rs.{trade.max_adverse:,.2f}.")
        if trade.max_favorable > abs(trade.net_pnl) and trade.net_pnl < 0:
            lines.append(
                f"Note: Was profitable (up to Rs.{trade.max_favorable:,.2f}) before reversing.")
    return " ".join(lines)


def _compute_overview_data(result: BacktestResult, metrics: PerformanceMetrics,
                            initial_capital: float) -> dict:
    """Compute all overview metrics: daily stats, hourly, weekly, drawdown, streaks."""
    trades = result.trades

    # ── Daily stats ──
    daily = defaultdict(lambda: {"pnl": 0, "capital_used": 0, "trades": 0, "wins": 0,
                                  "charges": 0, "gross_pnl": 0})
    for t in trades:
        day = t.entry_time.strftime("%Y-%m-%d") if t.entry_time else "unknown"
        daily[day]["pnl"] += t.net_pnl
        daily[day]["gross_pnl"] += t.pnl
        daily[day]["charges"] += t.charges
        daily[day]["capital_used"] += t.entry_price * t.original_qty
        daily[day]["trades"] += 1
        if t.net_pnl > 0:
            daily[day]["wins"] += 1

    sorted_days = sorted(daily.keys())
    cum_pnl = 0
    daily_list = []
    for d in sorted_days:
        dd = daily[d]
        cum_pnl += dd["pnl"]
        wr = (dd["wins"] / dd["trades"] * 100) if dd["trades"] > 0 else 0
        roi = (dd["pnl"] / dd["capital_used"] * 100) if dd["capital_used"] > 0 else 0
        daily_list.append({
            "date": d,
            "pnl": round(dd["pnl"], 2),
            "cum_pnl": round(cum_pnl, 2),
            "capital_used": round(dd["capital_used"], 2),
            "trades": dd["trades"],
            "wins": dd["wins"],
            "losses": dd["trades"] - dd["wins"],
            "win_rate": round(wr, 1),
            "charges": round(dd["charges"], 2),
            "roi": round(roi, 2),
        })

    # ── Hourly stats ──
    hourly = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
    for t in trades:
        h = t.entry_time.hour if t.entry_time else 0
        hourly[h]["pnl"] += t.net_pnl
        hourly[h]["trades"] += 1
        if t.net_pnl > 0:
            hourly[h]["wins"] += 1
    hourly_list = []
    for h in range(9, 16):
        hd = hourly[h]
        wr = (hd["wins"] / hd["trades"] * 100) if hd["trades"] > 0 else 0
        hourly_list.append({
            "hour": f"{h:02d}:00",
            "pnl": round(hd["pnl"], 2),
            "trades": hd["trades"],
            "win_rate": round(wr, 1),
        })

    # ── Day-of-week stats ──
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0, "capital": 0})
    for t in trades:
        d = t.entry_time.weekday() if t.entry_time else 0
        dow[d]["pnl"] += t.net_pnl
        dow[d]["trades"] += 1
        dow[d]["capital"] += t.entry_price * t.original_qty
        if t.net_pnl > 0:
            dow[d]["wins"] += 1
    dow_list = []
    for d in range(5):
        dd = dow[d]
        wr = (dd["wins"] / dd["trades"] * 100) if dd["trades"] > 0 else 0
        dow_list.append({
            "day": dow_names[d],
            "pnl": round(dd["pnl"], 2),
            "trades": dd["trades"],
            "win_rate": round(wr, 1),
            "avg_pnl": round(dd["pnl"] / dd["trades"], 2) if dd["trades"] > 0 else 0,
        })

    # ── Drawdown series ──
    equity = [initial_capital]
    for t in trades:
        equity.append(equity[-1] + t.net_pnl)
    equity_arr = np.array(equity)
    peak = np.maximum.accumulate(equity_arr)
    drawdown = ((equity_arr - peak) / peak * 100).tolist()
    drawdown_values = [round(d, 2) for d in drawdown[1:]]  # skip initial

    # ── Win/Loss streak ──
    streaks = []
    current_streak = 0
    for t in trades:
        if t.net_pnl > 0:
            current_streak = current_streak + 1 if current_streak > 0 else 1
        else:
            current_streak = current_streak - 1 if current_streak < 0 else -1
        streaks.append(current_streak)

    # ── Cumulative P&L by strategy ──
    strat_cum = defaultdict(list)
    strat_running = defaultdict(float)
    for t in trades:
        strat_running[t.strategy] += t.net_pnl
        strat_cum[t.strategy].append(round(strat_running[t.strategy], 2))
    strat_cum_dict = dict(strat_cum)

    # ── Rolling win rate (last 10 trades) ──
    rolling_wr = []
    for i in range(len(trades)):
        window = trades[max(0, i - 9):i + 1]
        wins = sum(1 for t in window if t.net_pnl > 0)
        rolling_wr.append(round(wins / len(window) * 100, 1))

    # ── Exit reason P&L breakdown ──
    exit_pnl = defaultdict(lambda: {"pnl": 0, "count": 0})
    for t in trades:
        exit_pnl[t.exit_reason]["pnl"] += t.net_pnl
        exit_pnl[t.exit_reason]["count"] += 1
    exit_pnl_list = [{"reason": k, "pnl": round(v["pnl"], 2), "count": v["count"],
                       "avg": round(v["pnl"] / v["count"], 2) if v["count"] > 0 else 0}
                      for k, v in sorted(exit_pnl.items(), key=lambda x: -x[1]["pnl"])]

    # ── Charge breakdown ──
    total_capital = sum(t.entry_price * t.original_qty for t in trades)
    charge_pct = (metrics.total_charges / total_capital * 100) if total_capital > 0 else 0

    # ── Capital efficiency ──
    avg_daily_capital = np.mean([d["capital_used"] for d in daily_list]) if daily_list else 0
    max_daily_capital = max((d["capital_used"] for d in daily_list), default=0)
    capital_utilization = (avg_daily_capital / initial_capital * 100) if initial_capital > 0 else 0

    return {
        "daily": daily_list,
        "hourly": hourly_list,
        "dow": dow_list,
        "drawdown": drawdown_values,
        "streaks": streaks,
        "strat_cum": strat_cum_dict,
        "rolling_wr": rolling_wr,
        "exit_pnl": exit_pnl_list,
        "charge_pct": round(charge_pct, 3),
        "avg_daily_capital": round(avg_daily_capital, 0),
        "max_daily_capital": round(max_daily_capital, 0),
        "capital_utilization": round(capital_utilization, 1),
        "total_capital_deployed": round(total_capital, 0),
    }


def generate_dashboard(result: BacktestResult, metrics: PerformanceMetrics,
                       output_path: str = None, initial_capital: float = 100_000) -> str:
    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"data/reports/dashboard_{ts}.html"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    overview = _compute_overview_data(result, metrics, initial_capital)

    trades_json = []
    for t in result.trades:
        ctx = t.context or {}
        narrative = _generate_narrative(t)
        holding_mins = (t.exit_time - t.entry_time).total_seconds() / 60 if t.exit_time and t.entry_time else 0
        # Find exit candle index in the option price data
        exit_candle_index = -1
        opt_prices = ctx.get("opt_prices", {})
        if t.exit_time and opt_prices.get("dates"):
            exit_ts = t.exit_time.isoformat() if hasattr(t.exit_time, 'isoformat') else str(t.exit_time)
            for idx_e, d in enumerate(opt_prices["dates"]):
                if d <= exit_ts:
                    exit_candle_index = idx_e

        trades_json.append({
            "id": t.trade_id, "symbol": t.tradingsymbol, "strategy": t.strategy,
            "action": t.action, "entry": t.entry_price, "exit": t.exit_price,
            "sl": t.stop_loss, "target": t.target,
            "qty": t.original_qty, "lots": t.original_qty // t.lot_size if t.lot_size else 1,
            "pnl": round(t.net_pnl, 2), "charges": round(t.charges, 2), "gross_pnl": round(t.pnl, 2),
            "reason": t.exit_reason,
            "entry_time": t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "",
            "exit_time": t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else "",
            "holding": round(holding_mins), "score": round(t.score, 1),
            "trend_strength": round(t.trend_strength, 1),
            "mfe": round(t.max_favorable, 2), "mae": round(t.max_adverse, 2),
            "narrative": narrative,
            "context": {
                "spot_ohlc": ctx.get("spot_ohlc", {}), "ema9": ctx.get("ema9", []),
                "ema21": ctx.get("ema21", []), "rsi": ctx.get("rsi", []),
                "macd_line": ctx.get("macd_line", []), "macd_signal": ctx.get("macd_signal", []),
                "macd_hist": ctx.get("macd_hist", []), "supertrend": ctx.get("supertrend", []),
                "supertrend_dir": ctx.get("supertrend_dir", []), "atr": ctx.get("atr", []),
                "opt_prices": ctx.get("opt_prices", {}),
                "entry_candle_index": ctx.get("entry_candle_index", 0),
                "exit_candle_index": exit_candle_index,
                "trend": ctx.get("trend", {}), "score_breakdown": ctx.get("score_breakdown", {}),
            },
        })

    eq_dates = [e["date"].isoformat() if hasattr(e["date"], 'isoformat') else str(e["date"])
                for e in result.equity_curve]
    eq_values = [round(e["equity"], 2) for e in result.equity_curve]
    if len(eq_dates) > 2000:
        step = len(eq_dates) // 2000
        eq_dates, eq_values = eq_dates[::step], eq_values[::step]

    m = metrics
    html = _build_html(trades_json, eq_dates, eq_values, m, overview, initial_capital)
    with open(output_path, "w") as f:
        f.write(html)
    return output_path


def _build_html(trades_json, eq_dates, eq_values, m, ov, initial_capital):
    pnl_cls = "g" if m.net_pnl >= 0 else "r"
    sharpe_cls = "g" if m.sharpe_ratio > 1 else ("o" if m.sharpe_ratio > 0 else "r")
    pf_cls = "g" if m.profit_factor > 1.5 else ("o" if m.profit_factor > 1 else "r")
    sortino_cls = "g" if m.sortino_ratio > 1 else ("o" if m.sortino_ratio > 0 else "r")
    wr_cls = "g" if m.win_rate > 50 else ("o" if m.win_rate > 40 else "r")

    # Pre-build exit reason table rows
    exit_rows = ""
    for e in ov["exit_pnl"]:
        cls = "pnl-pos" if e["pnl"] >= 0 else "pnl-neg"
        exit_rows += (f'<tr><td>{e["reason"].replace("_"," ")}</td><td>{e["count"]}</td>'
                      f'<td class="{cls}">{e["pnl"]:+,.0f}</td>'
                      f'<td class="{cls}">{e["avg"]:+,.0f}</td></tr>\n')

    # Pre-build daily table rows
    daily_rows = ""
    for d in ov["daily"]:
        cls = "pnl-pos" if d["pnl"] >= 0 else "pnl-neg"
        daily_rows += (f'<tr><td>{d["date"]}</td><td>{d["trades"]}</td>'
                       f'<td>{d["wins"]}/{d["losses"]}</td><td>{d["win_rate"]}%</td>'
                       f'<td class="{cls}">{d["pnl"]:+,.0f}</td>'
                       f'<td>{d["capital_used"]:,.0f}</td><td>{d["roi"]:+.1f}%</td>'
                       f'<td>{d["charges"]:.0f}</td></tr>\n')

    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Trading Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0e1a;color:#e0e0e0;font-size:14px;}
a{color:#00d4ff;} h2{color:#00d4ff;font-size:1.1em;margin:0 0 10px;}
.header{padding:14px 24px;border-bottom:1px solid #1a2040;display:flex;justify-content:space-between;align-items:center;}
.header h1{font-size:1.5em;background:linear-gradient(135deg,#00d4ff,#7b2ff7);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.header .meta{color:#6b7280;font-size:0.8em;}
.tab-bar{display:flex;border-bottom:1px solid #1a2040;background:#0d1117;padding:0 16px;overflow-x:auto;}
.tab{padding:10px 18px;cursor:pointer;color:#6b7280;font-size:0.85em;font-weight:600;border-bottom:2px solid transparent;white-space:nowrap;}
.tab:hover{color:#e0e0e0;} .tab.active{color:#00d4ff;border-bottom-color:#00d4ff;}
.tab-content{display:none;} .tab-content.active{display:block;}

/* KPI */
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;padding:12px 16px;}
.kpi{background:#111827;border:1px solid #1f2937;border-radius:8px;padding:10px 12px;text-align:center;}
.kpi .v{font-size:1.15em;font-weight:800;} .kpi .l{font-size:0.68em;color:#6b7280;margin-top:2px;}
.kpi.g .v{color:#10b981;} .kpi.r .v{color:#ef4444;} .kpi.b .v{color:#3b82f6;}
.kpi.p .v{color:#a78bfa;} .kpi.o .v{color:#f59e0b;} .kpi.w .v{color:#fff;}

.chart-box{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:14px;margin:8px 0;}
.chart-box h3{font-size:0.85em;color:#00d4ff;margin-bottom:8px;}
.chart-box canvas{max-height:220px;}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:0 16px;}
.grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;padding:0 16px;}
@media(max-width:900px){.grid-2,.grid-3{grid-template-columns:1fr;}}
.pad{padding:8px 16px;}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:0.82em;}
th{background:#1a2240;color:#00d4ff;padding:8px;text-align:left;position:sticky;top:0;z-index:1;}
td{padding:7px 8px;border-bottom:1px solid #111827;} tr:hover td{background:#131830;}
.pnl-pos{color:#10b981;font-weight:600;} .pnl-neg{color:#ef4444;font-weight:600;}
.table-wrap{max-height:350px;overflow-y:auto;border:1px solid #1f2937;border-radius:10px;margin:8px 0;}

/* Trade list + detail (bottom section) */
.main-layout{display:grid;grid-template-columns:380px 1fr;border-top:1px solid #1a2040;}
@media(max-width:900px){.main-layout{grid-template-columns:1fr;}}
.sidebar{border-right:1px solid #1a2040;overflow-y:auto;max-height:600px;background:#0d1117;}
.search-bar{padding:8px 10px;border-bottom:1px solid #1a2040;}
.search-bar input{width:100%;background:#111827;border:1px solid #253050;color:#e0e0e0;padding:7px 10px;border-radius:6px;font-size:0.82em;}
.trade-item{display:grid;grid-template-columns:1fr auto;padding:10px 12px;border-bottom:1px solid #111827;cursor:pointer;transition:background 0.12s;}
.trade-item:hover{background:#131830;} .trade-item.active{background:#1a2240;border-left:3px solid #00d4ff;}
.trade-item .ti-sym{font-weight:600;font-size:0.85em;color:#fff;}
.trade-item .ti-meta{font-size:0.7em;color:#6b7280;margin-top:2px;}
.trade-item .ti-pnl{font-weight:700;font-size:0.9em;text-align:right;}
.trade-item .ti-reason{font-size:0.65em;color:#6b7280;text-align:right;}
.detail{overflow-y:auto;max-height:600px;padding:0;}
.detail-empty{display:flex;align-items:center;justify-content:center;height:300px;color:#374151;font-size:1.1em;}
.detail-content{display:none;padding:16px;}
.detail-header{display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:12px;}
.dh-title{font-size:1.2em;font-weight:700;color:#fff;} .dh-pnl{font-size:1.3em;font-weight:800;}
.dh-meta{font-size:0.78em;color:#9ca3af;}
.dk-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:6px;margin:10px 0;}
.dk{background:#111827;border:1px solid #1f2937;border-radius:6px;padding:8px;text-align:center;}
.dk .dkv{font-size:1em;font-weight:700;} .dk .dkl{font-size:0.65em;color:#6b7280;margin-top:2px;}
.ind-tags{display:flex;flex-wrap:wrap;gap:5px;margin:8px 0;}
.ind-tag{padding:3px 9px;border-radius:16px;font-size:0.72em;font-weight:600;}
.ind-tag.bull{background:#064e3b;color:#10b981;border:1px solid #10b981;}
.ind-tag.bear{background:#450a0a;color:#ef4444;border:1px solid #ef4444;}
.ind-tag.neutral{background:#1c1917;color:#a3a3a3;border:1px solid #525252;}
.score-row{display:flex;align-items:center;margin:3px 0;font-size:0.78em;}
.score-row .sr-l{width:110px;color:#9ca3af;} .score-row .sr-b{flex:1;background:#1a2240;height:7px;border-radius:3px;overflow:hidden;margin:0 6px;}
.score-row .sr-f{height:100%;border-radius:3px;} .score-row .sr-v{width:35px;text-align:right;color:#fff;font-weight:600;}
.narrative{background:linear-gradient(135deg,#0f1629,#151d35);border:1px solid #253050;border-radius:10px;padding:16px;margin:12px 0;line-height:1.65;font-size:0.85em;color:#d1d5db;}
.narrative h3{color:#a78bfa;margin-bottom:8px;font-size:0.95em;}
.timeline{margin:10px 0;position:relative;padding-left:18px;}
.timeline::before{content:'';position:absolute;left:7px;top:0;bottom:0;width:2px;background:#253050;}
.tl-item{position:relative;margin-bottom:10px;padding-left:14px;}
.tl-item::before{content:'';position:absolute;left:-14px;top:4px;width:9px;height:9px;border-radius:50%;border:2px solid #0a0e1a;}
.tl-item.entry::before{background:#3b82f6;} .tl-item.sl::before{background:#ef4444;}
.tl-item.target::before{background:#10b981;} .tl-item.trailing::before{background:#f59e0b;}
.tl-item.exit::before{background:#a78bfa;}
.tl-item .tl-time{font-size:0.68em;color:#6b7280;} .tl-item .tl-text{font-size:0.8em;}
.chart-row-d{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
@media(max-width:1100px){.chart-row-d{grid-template-columns:1fr;}}
</style></head><body>

<div class="header">
  <h1>Trading Dashboard</h1>
  <div class="meta">""" + f'{m.total_trades} trades | {m.trading_days} days | Capital: {initial_capital:,.0f} | Generated {datetime.now().strftime("%Y-%m-%d %H:%M")}' + """</div>
</div>

<!-- ═══ TAB BAR ═══ -->
<div class="tab-bar">
  <div class="tab active" onclick="switchTab('overview')">Overview</div>
  <div class="tab" onclick="switchTab('daily')">Daily Breakdown</div>
  <div class="tab" onclick="switchTab('analysis')">Performance Analysis</div>
  <div class="tab" onclick="switchTab('trades')">Trade Explorer</div>
</div>

<!-- ═══ TAB 1: OVERVIEW ═══ -->
<div class="tab-content active" id="tab-overview">

<div class="kpi-row">
  <div class="kpi """ + pnl_cls + '"><div class="v">' + f'{m.net_pnl:+,.0f}' + '</div><div class="l">Net P&L</div></div>' + """
  <div class="kpi """ + pnl_cls + '"><div class="v">' + f'{m.net_return_pct:+.1f}%' + '</div><div class="l">Return</div></div>' + """
  <div class="kpi """ + sharpe_cls + '"><div class="v">' + f'{m.sharpe_ratio}' + '</div><div class="l">Sharpe</div></div>' + """
  <div class="kpi """ + sortino_cls + '"><div class="v">' + f'{m.sortino_ratio}' + '</div><div class="l">Sortino</div></div>' + """
  <div class="kpi """ + pf_cls + '"><div class="v">' + f'{m.profit_factor}' + '</div><div class="l">Profit Factor</div></div>' + """
  <div class="kpi """ + wr_cls + '"><div class="v">' + f'{m.win_rate}%' + '</div><div class="l">Win Rate ({m.winners}W/{m.losers}L)</div></div>' + """
  <div class="kpi r"><div class="v">""" + f'{m.max_drawdown:,.0f}' + """</div><div class="l">Max Drawdown</div></div>
  <div class="kpi r"><div class="v">""" + f'{m.max_drawdown_pct:.1f}%' + """</div><div class="l">Max DD %</div></div>
  <div class="kpi b"><div class="v">""" + f'{m.expectancy:+,.0f}' + """</div><div class="l">Expectancy/Trade</div></div>
  <div class="kpi p"><div class="v">""" + f'{m.annualized_return:+.0f}%' + """</div><div class="l">Annualized</div></div>
  <div class="kpi o"><div class="v">""" + f'{m.calmar_ratio:.2f}' + """</div><div class="l">Calmar</div></div>
  <div class="kpi w"><div class="v">""" + f'{m.total_trades}' + """</div><div class="l">Total Trades</div></div>
</div>
<div class="kpi-row">
  <div class="kpi g"><div class="v">""" + f'{m.avg_winner:+,.0f}' + """</div><div class="l">Avg Winner</div></div>
  <div class="kpi r"><div class="v">""" + f'{m.avg_loser:,.0f}' + """</div><div class="l">Avg Loser</div></div>
  <div class="kpi g"><div class="v">""" + f'{m.largest_winner:+,.0f}' + """</div><div class="l">Best Trade</div></div>
  <div class="kpi r"><div class="v">""" + f'{m.largest_loser:,.0f}' + """</div><div class="l">Worst Trade</div></div>
  <div class="kpi g"><div class="v">""" + f'{m.best_day:+,.0f}' + """</div><div class="l">Best Day</div></div>
  <div class="kpi r"><div class="v">""" + f'{m.worst_day:,.0f}' + """</div><div class="l">Worst Day</div></div>
  <div class="kpi b"><div class="v">""" + f'{m.avg_daily_pnl:+,.0f}' + """</div><div class="l">Avg Daily P&L</div></div>
  <div class="kpi g"><div class="v">""" + f'{m.profitable_days}/{m.trading_days}' + """</div><div class="l">Profitable Days</div></div>
  <div class="kpi o"><div class="v">""" + f'{m.avg_holding_minutes:.0f}m' + """</div><div class="l">Avg Holding</div></div>
  <div class="kpi p"><div class="v">""" + f'{ov["capital_utilization"]}%' + """</div><div class="l">Capital Usage</div></div>
  <div class="kpi o"><div class="v">""" + f'{m.total_charges:,.0f}' + """</div><div class="l">Total Charges</div></div>
  <div class="kpi b"><div class="v">""" + f'{m.max_consecutive_wins}W/{m.max_consecutive_losses}L' + """</div><div class="l">Max Streak</div></div>
</div>

<div class="grid-2">
  <div class="chart-box"><h3>Equity Curve</h3><canvas id="eqChart"></canvas></div>
  <div class="chart-box"><h3>Drawdown %</h3><canvas id="ddChart"></canvas></div>
</div>
</div>

<!-- ═══ TAB 2: DAILY BREAKDOWN ═══ -->
<div class="tab-content" id="tab-daily">
<div class="grid-2" style="margin-top:10px;">
  <div class="chart-box"><h3>Daily P&L</h3><canvas id="dailyPnlChart"></canvas></div>
  <div class="chart-box"><h3>Daily Capital Deployed</h3><canvas id="dailyCapChart"></canvas></div>
</div>
<div class="grid-2">
  <div class="chart-box"><h3>Cumulative P&L</h3><canvas id="cumPnlChart"></canvas></div>
  <div class="chart-box"><h3>Daily ROI %</h3><canvas id="dailyRoiChart"></canvas></div>
</div>
<div class="pad">
  <h2>Day-wise Breakdown</h2>
  <div class="table-wrap">
  <table><tr><th>Date</th><th>Trades</th><th>W/L</th><th>Win Rate</th><th>P&L</th><th>Capital Used</th><th>ROI</th><th>Charges</th></tr>
  """ + daily_rows + """
  </table></div>
</div>
</div>

<!-- ═══ TAB 3: PERFORMANCE ANALYSIS ═══ -->
<div class="tab-content" id="tab-analysis">
<div class="grid-3" style="margin-top:10px;">
  <div class="chart-box"><h3>Hourly Performance</h3><canvas id="hourlyChart"></canvas></div>
  <div class="chart-box"><h3>Day-of-Week P&L</h3><canvas id="dowChart"></canvas></div>
  <div class="chart-box"><h3>Win/Loss Streak</h3><canvas id="streakChart"></canvas></div>
</div>
<div class="grid-2">
  <div class="chart-box"><h3>Rolling Win Rate (10-trade)</h3><canvas id="rollingWrChart"></canvas></div>
  <div class="chart-box"><h3>P&L by Strategy (Cumulative)</h3><canvas id="stratCumChart"></canvas></div>
</div>
<div class="grid-2">
  <div class="chart-box"><h3>P&L Distribution</h3><canvas id="pnlDistChart"></canvas></div>
  <div>
    <div class="chart-box">
      <h3>Exit Reason Breakdown</h3>
      <div class="table-wrap" style="max-height:200px;">
      <table><tr><th>Reason</th><th>Count</th><th>Total P&L</th><th>Avg P&L</th></tr>
      """ + exit_rows + """
      </table></div>
    </div>
  </div>
</div>
</div>

<!-- ═══ TAB 4: TRADE EXPLORER ═══ -->
<div class="tab-content" id="tab-trades">
<div class="main-layout">
  <div class="sidebar">
    <div class="search-bar"><input type="text" id="searchInput" placeholder="Search trades..." oninput="filterTrades()"></div>
    <div id="tradeList"></div>
  </div>
  <div class="detail" id="detailPanel">
    <div class="detail-empty" id="emptyState">Click a trade to see full analysis</div>
    <div class="detail-content" id="detailContent"></div>
  </div>
</div>
</div>

<script>
const TRADES=""" + json.dumps(trades_json, default=str) + """;
const EQ_DATES=""" + json.dumps(eq_dates) + """;
const EQ_VALUES=""" + json.dumps(eq_values) + """;
const OV=""" + json.dumps(ov, default=str) + """;

let activeCharts=[], overviewChartsDrawn=false, dailyChartsDrawn=false, analysisChartsDrawn=false;
const CS={responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#9ca3af',font:{size:10}}}},
  scales:{x:{ticks:{color:'#4b5563',maxTicksLimit:12,font:{size:9}},grid:{color:'#111827'}},y:{ticks:{color:'#6b7280'},grid:{color:'#1a2040'}}}};

// ── Tabs ──
function switchTab(id){
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',t.textContent.toLowerCase().includes(id.substring(0,4))));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  if(id==='overview'&&!overviewChartsDrawn){drawOverviewCharts();overviewChartsDrawn=true;}
  if(id==='daily'&&!dailyChartsDrawn){drawDailyCharts();dailyChartsDrawn=true;}
  if(id==='analysis'&&!analysisChartsDrawn){drawAnalysisCharts();analysisChartsDrawn=true;}
}

// ── Overview Charts ──
function drawOverviewCharts(){
  new Chart(document.getElementById('eqChart'),{type:'line',data:{labels:EQ_DATES.map(d=>d.substring(0,10)),
    datasets:[{label:'Equity',data:EQ_VALUES,borderColor:'#00d4ff',backgroundColor:'rgba(0,212,255,0.08)',fill:true,pointRadius:0,borderWidth:1.5}]},options:CS});
  const ddLabels=Array.from({length:OV.drawdown.length},(_,i)=>i);
  new Chart(document.getElementById('ddChart'),{type:'line',data:{labels:ddLabels,
    datasets:[{label:'Drawdown %',data:OV.drawdown,borderColor:'#ef4444',backgroundColor:'rgba(239,68,68,0.1)',fill:true,pointRadius:0,borderWidth:1.5}]},
    options:{...CS,scales:{...CS.scales,x:{display:false},y:{ticks:{color:'#6b7280'},grid:{color:'#1a2040'}}}}});
}

// ── Daily Charts ──
function drawDailyCharts(){
  const dd=OV.daily, dates=dd.map(d=>d.date), pnls=dd.map(d=>d.pnl), caps=dd.map(d=>d.capital_used);
  const cumPnl=dd.map(d=>d.cum_pnl), rois=dd.map(d=>d.roi);
  const pnlColors=pnls.map(p=>p>=0?'#10b981':'#ef4444');
  const roiColors=rois.map(r=>r>=0?'#3b82f6':'#ef4444');
  new Chart(document.getElementById('dailyPnlChart'),{type:'bar',data:{labels:dates,
    datasets:[{label:'P&L',data:pnls,backgroundColor:pnlColors}]},options:CS});
  new Chart(document.getElementById('dailyCapChart'),{type:'bar',data:{labels:dates,
    datasets:[{label:'Capital Used',data:caps,backgroundColor:'#3b82f680',borderColor:'#3b82f6',borderWidth:1}]},options:CS});
  new Chart(document.getElementById('cumPnlChart'),{type:'line',data:{labels:dates,
    datasets:[{label:'Cumulative P&L',data:cumPnl,borderColor:'#a78bfa',backgroundColor:'rgba(167,139,250,0.1)',fill:true,pointRadius:1,borderWidth:1.5}]},options:CS});
  new Chart(document.getElementById('dailyRoiChart'),{type:'bar',data:{labels:dates,
    datasets:[{label:'ROI %',data:rois,backgroundColor:roiColors}]},options:CS});
}

// ── Analysis Charts ──
function drawAnalysisCharts(){
  // Hourly
  const hLabels=OV.hourly.map(h=>h.hour), hPnl=OV.hourly.map(h=>h.pnl), hTrades=OV.hourly.map(h=>h.trades);
  const hWr=OV.hourly.map(h=>h.win_rate), hColors=hPnl.map(p=>p>=0?'#10b981':'#ef4444');
  new Chart(document.getElementById('hourlyChart'),{type:'bar',data:{labels:hLabels,datasets:[
    {type:'bar',label:'P&L',data:hPnl,backgroundColor:hColors,yAxisID:'y'},
    {type:'line',label:'Win Rate %',data:hWr,borderColor:'#f59e0b',pointRadius:3,borderWidth:2,yAxisID:'y1'},
  ]},options:{...CS,scales:{...CS.scales,y1:{position:'right',ticks:{color:'#f59e0b'},grid:{display:false},min:0,max:100}}}});

  // Day of week
  const dLabels=OV.dow.map(d=>d.day), dPnl=OV.dow.map(d=>d.pnl), dColors=dPnl.map(p=>p>=0?'#10b981':'#ef4444');
  new Chart(document.getElementById('dowChart'),{type:'bar',data:{labels:dLabels,
    datasets:[{label:'P&L',data:dPnl,backgroundColor:dColors}]},options:CS});

  // Streaks
  const sColors=OV.streaks.map(s=>s>0?'#10b981':'#ef4444');
  new Chart(document.getElementById('streakChart'),{type:'bar',data:{labels:OV.streaks.map((_,i)=>i+1),
    datasets:[{label:'Streak',data:OV.streaks,backgroundColor:sColors}]},
    options:{...CS,scales:{...CS.scales,x:{display:false}}}});

  // Rolling WR
  new Chart(document.getElementById('rollingWrChart'),{type:'line',data:{labels:OV.rolling_wr.map((_,i)=>i+1),
    datasets:[{label:'Win Rate %',data:OV.rolling_wr,borderColor:'#00d4ff',pointRadius:0,borderWidth:1.5,fill:false},
      {label:'50%',data:Array(OV.rolling_wr.length).fill(50),borderColor:'#374151',borderDash:[4,4],pointRadius:0,borderWidth:1}
    ]},options:{...CS,scales:{...CS.scales,x:{display:false},y:{min:0,max:100,ticks:{color:'#6b7280'},grid:{color:'#1a2040'}}}}});

  // Strategy cumulative
  const stratDs=[];const colors=['#3b82f6','#10b981','#f59e0b','#a78bfa','#ef4444'];let ci=0;
  for(const[name,vals]of Object.entries(OV.strat_cum)){
    stratDs.push({label:name,data:vals,borderColor:colors[ci%5],pointRadius:0,borderWidth:2,fill:false});ci++;}
  new Chart(document.getElementById('stratCumChart'),{type:'line',data:{labels:stratDs[0]?.data.map((_,i)=>i+1)||[],
    datasets:stratDs},options:{...CS,scales:{...CS.scales,x:{display:false}}}});

  // P&L dist
  const allPnl=TRADES.map(t=>t.pnl),distColors=allPnl.map(p=>p>=0?'#10b981':'#ef4444');
  new Chart(document.getElementById('pnlDistChart'),{type:'bar',data:{labels:allPnl.map((_,i)=>i+1),
    datasets:[{label:'Trade P&L',data:allPnl,backgroundColor:distColors}]},
    options:{...CS,scales:{...CS.scales,x:{display:false}}}});
}

// ── Trade List ──
function renderTradeList(trades){
  const list=document.getElementById('tradeList');list.innerHTML='';
  trades.forEach(t=>{
    const div=document.createElement('div');div.className='trade-item';div.dataset.id=t.id;
    div.onclick=()=>showTradeDetail(t);
    const c=t.pnl>=0?'pnl-pos':'pnl-neg';
    div.innerHTML=`<div><div class="ti-sym">#${t.id} ${t.symbol.substring(0,20)}</div>
      <div class="ti-meta">${t.strategy} | ${t.entry_time} | ${t.holding}m</div></div>
      <div><div class="ti-pnl ${c}">${t.pnl>=0?'+':''}${t.pnl.toLocaleString('en-IN',{maximumFractionDigits:0})}</div>
      <div class="ti-reason">${t.reason.replace(/_/g,' ')}</div></div>`;
    list.appendChild(div);
  });
}
function filterTrades(){
  const q=document.getElementById('searchInput').value.toLowerCase();
  renderTradeList(TRADES.filter(t=>t.symbol.toLowerCase().includes(q)||t.strategy.includes(q)||t.entry_time.includes(q)||t.reason.toLowerCase().includes(q)));
}

// ── Trade Detail ──
let detailCharts=[];
function showTradeDetail(t){
  document.querySelectorAll('.trade-item').forEach(el=>el.classList.remove('active'));
  document.querySelector(`.trade-item[data-id="${t.id}"]`)?.classList.add('active');
  document.getElementById('emptyState').style.display='none';
  const panel=document.getElementById('detailContent');panel.style.display='block';
  detailCharts.forEach(c=>c.destroy());detailCharts=[];
  const pc=t.pnl>=0?'pnl-pos':'pnl-neg', risk=Math.abs(t.entry-t.sl), reward=Math.abs(t.target-t.entry);
  const rr=risk>0?(reward/risk).toFixed(2):'0', trend=t.context.trend||{}, details=trend.details||{};
  const trendDir=trend.direction===1?'BULLISH':'BEARISH';
  const trendColor=trend.direction===1?'#10b981':'#ef4444';
  const optType=t.symbol.includes('CE')?'Call (CE)':'Put (PE)';

  // Indicator tags with explanation
  let tagH='';
  const indExp={
    ema_cross:{name:'EMA 5/13',bull:'Fast EMA above slow = short-term momentum UP',bear:'Fast EMA below slow = momentum fading'},
    price_vs_ema50:{name:'Price vs EMA 20',bull:'Price above 20-EMA = above average, bullish',bear:'Price below 20-EMA = below average, bearish'},
    rsi:{name:'RSI (14)',bull:'RSI > 55 = buying pressure dominates',bear:'RSI < 45 = selling pressure dominates',neutral:'RSI 45-55 = no clear pressure'},
    macd:{name:'MACD',bull:'Histogram > 0 = bullish momentum accelerating',bear:'Histogram < 0 = bearish momentum accelerating'},
    supertrend:{name:'Supertrend (7,2.5)',bull:'Price above Supertrend line = uptrend intact',bear:'Price below Supertrend line = downtrend intact'},
    price_action:{name:'Price Action (3-bar)',bull:'Last 3 candles rising = immediate upward movement',bear:'Last 3 candles falling = immediate downward movement',neutral:'Flat = no clear short-term direction'}
  };
  let indDetailH='';
  let agreeCount=0, totalInd=0;
  for(const[k,v]of Object.entries(details)){
    const c=v==='BULL'?'bull':v==='BEAR'?'bear':'neutral';
    const inf=indExp[k]||{name:k};
    const agrees=(trendDir==='BULLISH'&&v==='BULL')||(trendDir==='BEARISH'&&v==='BEAR');
    if(agrees)agreeCount++;
    totalInd++;
    tagH+=`<span class="ind-tag ${c}">${inf.name}: ${v}</span>`;
    const exp=v==='BULL'?inf.bull:v==='BEAR'?inf.bear:(inf.neutral||'Neutral');
    const icon=agrees?'<span style="color:#10b981">&#10004;</span>':'<span style="color:#ef4444">&#10008;</span>';
    indDetailH+=`<div style="display:flex;gap:8px;align-items:flex-start;margin:6px 0;padding:8px 10px;background:#0f1629;border-radius:6px;border-left:3px solid ${v==='BULL'?'#10b981':v==='BEAR'?'#ef4444':'#525252'}">
      <div style="min-width:20px;text-align:center;font-size:1.1em;">${icon}</div>
      <div><div style="font-weight:700;font-size:0.85em;color:#fff;">${inf.name}: ${v}</div>
      <div style="font-size:0.78em;color:#9ca3af;">${exp}</div></div></div>`;
  }

  // Gate analysis
  const gateH=`<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:6px;margin:10px 0;">
    <div style="background:#064e3b;border:1px solid #10b981;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:0.7em;color:#6ee7b7;">GATE 1: Trend</div>
      <div style="font-weight:700;color:#10b981;">${trendDir} ${trend.strength||0}%</div></div>
    <div style="background:#064e3b;border:1px solid #10b981;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:0.7em;color:#6ee7b7;">GATE 1b: Not Exhausted</div>
      <div style="font-weight:700;color:#10b981;">${(trend.strength||0)<92?'PASS':'BORDERLINE'}</div></div>
    <div style="background:#064e3b;border:1px solid #10b981;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:0.7em;color:#6ee7b7;">GATE 2: RSI Guard</div>
      <div style="font-weight:700;color:#10b981;">RSI ${(trend.rsi||0).toFixed(0)} OK</div></div>
    <div style="background:#064e3b;border:1px solid #10b981;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:0.7em;color:#6ee7b7;">GATE 3: Momentum</div>
      <div style="font-weight:700;color:#10b981;">3-bar confirmed</div></div>
    <div style="background:#064e3b;border:1px solid #10b981;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:0.7em;color:#6ee7b7;">GATE 4: Daily Limit</div>
      <div style="font-weight:700;color:#10b981;">PASS</div></div>
    <div style="background:#064e3b;border:1px solid #10b981;border-radius:8px;padding:8px;text-align:center;">
      <div style="font-size:0.7em;color:#6ee7b7;">GATE 5: R:R Check</div>
      <div style="font-weight:700;color:#10b981;">1:${rr}</div></div>
  </div>`;

  // Score breakdown
  const sb=t.context.score_breakdown||{};let scH='';
  const sc={oi:'#10b981',volume:'#3b82f6',premium:'#f59e0b',trend_alignment:'#a78bfa',moneyness:'#ec4899'};
  const sm={oi:25,volume:20,premium:10,trend_alignment:25,moneyness:15};
  for(const[k,v]of Object.entries(sb)){const mx=sm[k]||25;const p=Math.min(v/mx*100,100);
    scH+=`<div class="score-row"><span class="sr-l">${k.replace(/_/g,' ')}</span><div class="sr-b"><div class="sr-f" style="width:${p}%;background:${sc[k]||'#3b82f6'}"></div></div><span class="sr-v">${Math.round(v)}/${mx}</span></div>`;}

  // SL calculation explanation
  const slPct=((t.entry-t.sl)/t.entry*100).toFixed(1);
  const slCalcH=`<div style="background:#0f1629;border:1px solid #253050;border-radius:8px;padding:12px;margin:8px 0;font-family:monospace;font-size:0.78em;color:#7dd3fc;line-height:1.6;">
SL = Entry - (ATR x Multiplier x Delta)<br>
SL = ${t.entry.toFixed(2)} - (ATR x 1.5 x 0.4)<br>
SL = <b style="color:#ef4444">${t.sl.toFixed(2)}</b> (${slPct}% below entry)<br><br>
Target = Entry + (Risk x R:R)<br>
Risk = ${t.entry.toFixed(2)} - ${t.sl.toFixed(2)} = ${risk.toFixed(2)}<br>
Target = ${t.entry.toFixed(2)} + (${risk.toFixed(2)} x ${rr}) = <b style="color:#10b981">${t.target.toFixed(2)}</b><br><br>
Position = MAX_LOSS / risk_per_unit / lot_size<br>
Position = 2000 / ${risk.toFixed(2)} = ${Math.floor(2000/risk)} units = <b>${t.lots} lot(s) x ${t.qty/t.lots} = ${t.qty} units</b></div>`;

  // Timeline
  let tlH=`<div class="tl-item entry"><div class="tl-time">${t.entry_time}</div><div class="tl-text"><b style="color:#3b82f6;">BUY</b> ${t.qty} units of ${t.symbol} @ <b>Rs.${t.entry.toFixed(2)}</b></div></div>
    <div class="tl-item sl"><div class="tl-text">Stop Loss placed at Rs.${t.sl.toFixed(2)} (${slPct}% below, max loss Rs.${(risk*t.qty).toFixed(0)})</div></div>
    <div class="tl-item target"><div class="tl-text">Target set at Rs.${t.target.toFixed(2)} (R:R 1:${rr}, potential Rs.${(reward*t.qty).toFixed(0)})</div></div>`;
  if(t.mfe>0)tlH+=`<div class="tl-item trailing"><div class="tl-text">Peak unrealized profit: <b style="color:#10b981;">+Rs.${t.mfe.toLocaleString('en-IN')}</b></div></div>`;
  if(t.pnl<0&&t.mfe>0)tlH+=`<div class="tl-item trailing"><div class="tl-text" style="color:#f59e0b;">Price reversed — gave back Rs.${(t.mfe+Math.abs(t.pnl)).toLocaleString('en-IN')} of profit</div></div>`;
  const exitIcon=t.pnl>=0?'<b style="color:#10b981;">SELL (profit)</b>':'<b style="color:#ef4444;">SELL (loss)</b>';
  tlH+=`<div class="tl-item exit"><div class="tl-time">${t.exit_time}</div><div class="tl-text">${exitIcon} @ <b>Rs.${t.exit.toFixed(2)}</b> | Reason: ${t.reason.replace(/_/g,' ')} | Net P&L: <b class="${pc}">Rs.${t.pnl>=0?'+':''}${t.pnl.toLocaleString('en-IN')}</b></div></div>`;

  // Outcome summary
  const outcomeColor=t.pnl>=0?'#064e3b':'#450a0a';
  const outcomeBorder=t.pnl>=0?'#10b981':'#ef4444';
  const outcomeH=`<div style="background:${outcomeColor};border:1px solid ${outcomeBorder};border-radius:10px;padding:14px;margin:10px 0;display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;">
    <div><div style="font-size:0.75em;color:#9ca3af;">Result</div><div style="font-size:1.5em;font-weight:800;color:${outcomeBorder};">Rs.${t.pnl>=0?'+':''}${t.pnl.toLocaleString('en-IN')}</div></div>
    <div><div style="font-size:0.75em;color:#9ca3af;">Gross P&L</div><div style="font-size:1.1em;font-weight:700;">${t.gross_pnl>=0?'+':''}${t.gross_pnl.toLocaleString('en-IN')}</div></div>
    <div><div style="font-size:0.75em;color:#9ca3af;">Charges</div><div style="font-size:1.1em;font-weight:700;">-${t.charges.toFixed(0)}</div></div>
    <div><div style="font-size:0.75em;color:#9ca3af;">Holding</div><div style="font-size:1.1em;font-weight:700;">${t.holding} min</div></div>
    <div><div style="font-size:0.75em;color:#9ca3af;">Exit</div><div style="font-size:1.1em;font-weight:700;">${t.reason.replace(/_/g,' ')}</div></div>
  </div>`;

  panel.innerHTML=`
    <div class="detail-header"><div><div class="dh-title">#${t.id} ${t.symbol}</div>
      <div class="dh-meta">${t.strategy.toUpperCase()} | ${optType} | ${t.entry_time} to ${t.exit_time} | ${t.holding} min</div></div>
      <div class="dh-pnl ${pc}">Rs.${t.pnl>=0?'+':''}${t.pnl.toLocaleString('en-IN')}</div></div>

    ${outcomeH}

    <div class="dk-grid">
      <div class="dk"><div class="dkv">${t.entry.toFixed(2)}</div><div class="dkl">Entry Price</div></div>
      <div class="dk"><div class="dkv">${t.exit.toFixed(2)}</div><div class="dkl">Exit Price</div></div>
      <div class="dk"><div class="dkv" style="color:#ef4444;">${t.sl.toFixed(2)}</div><div class="dkl">Stop Loss</div></div>
      <div class="dk"><div class="dkv" style="color:#10b981;">${t.target.toFixed(2)}</div><div class="dkl">Target</div></div>
      <div class="dk"><div class="dkv">1:${rr}</div><div class="dkl">Risk:Reward</div></div>
      <div class="dk"><div class="dkv">${t.score}</div><div class="dkl">Score</div></div>
      <div class="dk"><div class="dkv" style="color:${trendColor};">${t.trend_strength}%</div><div class="dkl">Trend Str.</div></div>
      <div class="dk"><div class="dkv">${t.qty} (${t.lots}L)</div><div class="dkl">Quantity</div></div>
      <div class="dk"><div class="dkv pnl-pos">+${t.mfe.toLocaleString('en-IN')}</div><div class="dkl">Peak Profit</div></div>
      <div class="dk"><div class="dkv pnl-neg">${t.mae.toLocaleString('en-IN')}</div><div class="dkl">Peak Loss</div></div>
    </div>

    <div style="margin:16px 0;"><div style="font-size:0.95em;font-weight:700;color:#00d4ff;margin-bottom:8px;">Option Premium — BUY & SELL Points</div></div>
    <div class="chart-box" style="height:280px;"><canvas id="optC"></canvas></div>

    <div style="font-size:0.95em;font-weight:700;color:#00d4ff;margin:16px 0 8px;">Spot Price + Indicators (BUY point marked)</div>
    <div class="chart-box" style="height:280px;"><canvas id="spotC"></canvas></div>

    <div class="chart-row-d">
      <div class="chart-box" style="height:220px;"><h3>RSI (14) at Entry: ${(trend.rsi||0).toFixed(1)}</h3><canvas id="rsiC"></canvas></div>
      <div class="chart-box" style="height:220px;"><h3>MACD Histogram</h3><canvas id="macdC"></canvas></div>
    </div>

    <div style="font-size:0.95em;font-weight:700;color:#f59e0b;margin:20px 0 8px;">Gate Analysis — All 6 Gates Passed</div>
    ${gateH}

    <div style="font-size:0.95em;font-weight:700;color:#a78bfa;margin:20px 0 8px;">Indicator Signals (${agreeCount}/${totalInd} agreed with ${trendDir} trade)</div>
    <div class="ind-tags">${tagH}</div>
    ${indDetailH}

    <div style="font-size:0.95em;font-weight:700;color:#a78bfa;margin:20px 0 8px;">Score Breakdown (${t.score}/100)</div>
    ${scH}

    <div style="font-size:0.95em;font-weight:700;color:#06b6d4;margin:20px 0 8px;">SL / Target / Position Sizing Calculation</div>
    ${slCalcH}

    <div style="font-size:0.95em;font-weight:700;color:#00d4ff;margin:20px 0 8px;">Trade Timeline</div>
    <div class="timeline">${tlH}</div>

    <div class="narrative"><h3>Full Trade Analysis</h3>${t.narrative}</div>`;

  // ── DRAW CHARTS WITH BUY/SELL MARKERS ──
  const cx=t.context;
  const sd=(cx.spot_ohlc.dates||[]).map(d=>d.substring(11,16)||d.substring(0,10));
  const ei=cx.entry_candle_index||0;

  // Option chart with BUY and SELL markers
  const od=(cx.opt_prices.dates||[]).map(d=>d.substring(11,16)||d);
  const exi=cx.exit_candle_index>=0?cx.exit_candle_index:-1;
  if(od.length>0){
    // Find closest entry/exit indices in option data
    const entryTime=t.entry_time.substring(11,16);
    const exitTime=t.exit_time.substring(11,16);
    let oei=-1,oxi=-1;
    for(let i=0;i<od.length;i++){if(od[i]<=entryTime)oei=i;if(od[i]<=exitTime)oxi=i;}
    if(oei<0)oei=0;if(oxi<0)oxi=od.length-1;

    const optPR=od.map((_,i)=>(i===oei||i===oxi)?9:0);
    const optPBg=od.map((_,i)=>i===oei?'#3b82f6':i===oxi?(t.pnl>=0?'#10b981':'#ef4444'):'transparent');
    const optPBc=od.map((_,i)=>i===oei?'#ffffff':i===oxi?'#ffffff':'transparent');
    const optPS=od.map((_,i)=>(i===oei||i===oxi)?'triangle':i===oxi?'triangle':'circle');

    detailCharts.push(new Chart(document.getElementById('optC'),{type:'line',data:{labels:od,datasets:[
      {label:'Premium',data:cx.opt_prices.close,borderColor:'#00d4ff',borderWidth:2,pointRadius:optPR,pointBackgroundColor:optPBg,pointBorderColor:optPBc,pointBorderWidth:2,pointStyle:optPS,fill:{target:'origin',above:'rgba(0,212,255,0.05)'}},
      {label:'Entry (BUY)',data:Array(od.length).fill(t.entry),borderColor:'#3b82f6',borderWidth:1.5,borderDash:[6,3],pointRadius:0},
      {label:'Stop Loss',data:Array(od.length).fill(t.sl),borderColor:'#ef4444',borderWidth:1.5,borderDash:[6,3],pointRadius:0},
      {label:'Target',data:Array(od.length).fill(t.target),borderColor:'#10b981',borderWidth:1.5,borderDash:[6,3],pointRadius:0},
      {label:'Exit (SELL)',data:Array(od.length).fill(t.exit),borderColor:t.pnl>=0?'#10b981':'#ef4444',borderWidth:1,borderDash:[2,4],pointRadius:0},
    ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#9ca3af',font:{size:10}}},
      tooltip:{callbacks:{label:function(ctx){
        if(ctx.dataIndex===oei&&ctx.datasetIndex===0)return 'BUY @ Rs.'+t.entry.toFixed(2);
        if(ctx.dataIndex===oxi&&ctx.datasetIndex===0)return 'SELL @ Rs.'+t.exit.toFixed(2)+' ('+t.reason.replace(/_/g,' ')+')';
        return ctx.dataset.label+': Rs.'+ctx.parsed.y.toFixed(2);}}}},
      scales:{x:{ticks:{color:'#4b5563',maxTicksLimit:15,font:{size:9}},grid:{color:'#111827'}},y:{ticks:{color:'#6b7280'},grid:{color:'#1a2040'}}}}}));
  }

  // Spot chart with BUY marker
  if(sd.length>0){
    const stC=(cx.supertrend_dir||[]).map(d=>d===1?'#10b981':'#ef4444');
    const pBg=sd.map((_,i)=>i===ei?'#3b82f6':'transparent');
    const pR=sd.map((_,i)=>i===ei?10:0);
    const pBc=sd.map((_,i)=>i===ei?'#ffffff':'transparent');
    detailCharts.push(new Chart(document.getElementById('spotC'),{type:'line',data:{labels:sd,datasets:[
      {label:'Spot Close',data:cx.spot_ohlc.close,borderColor:'#e0e0e0',borderWidth:1.5,pointRadius:pR,pointBackgroundColor:pBg,pointBorderColor:pBc,pointBorderWidth:2},
      {label:'EMA 5 (fast)',data:cx.ema9,borderColor:'#3b82f6',borderWidth:1.2,pointRadius:0},
      {label:'EMA 13 (slow)',data:cx.ema21,borderColor:'#f59e0b',borderWidth:1.2,pointRadius:0},
      {label:'Supertrend',data:cx.supertrend,borderColor:'#10b981',borderWidth:1.5,pointRadius:0,borderDash:[4,2],segment:{borderColor:ct=>stC[ct.p0DataIndex]||'#10b981'}},
    ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#9ca3af',font:{size:10}}},
      tooltip:{callbacks:{label:function(ctx){
        if(ctx.dataIndex===ei&&ctx.datasetIndex===0)return 'BUY SIGNAL HERE - Spot: '+ctx.parsed.y.toFixed(2);
        return ctx.dataset.label+': '+ctx.parsed.y.toFixed(2);}}}},
      scales:{x:{ticks:{color:'#4b5563',maxTicksLimit:15,font:{size:9}},grid:{color:'#111827'}},y:{ticks:{color:'#6b7280'},grid:{color:'#1a2040'}}}}}));
  }

  // RSI with entry marker and overbought/oversold zones
  if((cx.rsi||[]).length){
    const rsiPR=sd.map((_,i)=>i===ei?8:0);
    const rsiBg=sd.map((_,i)=>i===ei?'#a78bfa':'transparent');
    detailCharts.push(new Chart(document.getElementById('rsiC'),{type:'line',data:{labels:sd,datasets:[
      {label:'RSI',data:cx.rsi,borderColor:'#a78bfa',borderWidth:1.5,pointRadius:rsiPR,pointBackgroundColor:rsiBg,pointBorderColor:'#fff',pointBorderWidth:2},
      {label:'Overbought (70)',data:Array(sd.length).fill(70),borderColor:'#ef444480',borderWidth:1,borderDash:[4,4],pointRadius:0},
      {label:'Oversold (30)',data:Array(sd.length).fill(30),borderColor:'#10b98180',borderWidth:1,borderDash:[4,4],pointRadius:0},
    ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#9ca3af',font:{size:9}}}},
      scales:{x:{ticks:{color:'#4b5563',maxTicksLimit:10,font:{size:9}},grid:{color:'#111827'}},
        y:{min:0,max:100,ticks:{color:'#6b7280',stepSize:10},grid:{color:'#1a2040'}}}}}));}

  // MACD with entry marker
  if((cx.macd_line||[]).length){
    const hC=(cx.macd_hist||[]).map(v=>v>=0?'#10b981':'#ef4444');
    detailCharts.push(new Chart(document.getElementById('macdC'),{type:'bar',data:{labels:sd,datasets:[
      {type:'bar',label:'Histogram',data:cx.macd_hist,backgroundColor:hC,barPercentage:0.6},
      {type:'line',label:'MACD',data:cx.macd_line,borderColor:'#3b82f6',borderWidth:1.5,pointRadius:0},
      {type:'line',label:'Signal',data:cx.macd_signal,borderColor:'#f59e0b',borderWidth:1,pointRadius:0},
    ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#9ca3af',font:{size:9}}}},
      scales:{x:{ticks:{color:'#4b5563',maxTicksLimit:10,font:{size:9}},grid:{color:'#111827'}},y:{ticks:{color:'#6b7280'},grid:{color:'#1a2040'}}}}}));}

  document.getElementById('detailPanel').scrollTop=0;
}

// ── Init ──
renderTradeList(TRADES);
drawOverviewCharts();overviewChartsDrawn=true;
</script></body></html>"""
