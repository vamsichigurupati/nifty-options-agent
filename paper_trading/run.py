#!/usr/bin/env python3
"""
Run paper trading for all 3 strategy versions simultaneously.

Usage:
  # Live mode (polls Yahoo Finance every 5 min during market hours):
  python -m paper_trading.run --live

  # Simulate on recent data (run through last N days of real data):
  python -m paper_trading.run --simulate --days 20

  # Check status:
  python -m paper_trading.run --status

  # Reset all paper trades:
  python -m paper_trading.run --reset

  # Generate comparison dashboard:
  python -m paper_trading.run --dashboard
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paper_trading.versions import VERSION_CONFIG
from paper_trading.trader import PaperTrader, TRADES_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def create_traders(capital: float = 50_000) -> dict:
    traders = {}
    for vid, cfg in VERSION_CONFIG.items():
        detect_fn = cfg["detect_trend"]()
        traders[vid] = PaperTrader(vid, detect_fn, capital=capital)
    return traders


def fetch_spot_data(period="5d", interval="5m") -> pd.DataFrame:
    """Fetch latest NIFTY data from Yahoo Finance."""
    nifty = yf.Ticker("^NSEI")
    raw = nifty.history(period=period, interval=interval)
    if raw.empty:
        return pd.DataFrame()
    raw = raw.reset_index()
    if "Datetime" in raw.columns:
        raw = raw.rename(columns={"Datetime": "date"})
    elif "Date" in raw.columns:
        raw = raw.rename(columns={"Date": "date"})
    raw = raw.rename(columns={"Open": "open", "High": "high", "Low": "low",
                               "Close": "close", "Volume": "volume"})
    raw["date"] = pd.to_datetime(raw["date"]).dt.tz_localize(None)
    raw = raw[(raw["date"].dt.hour >= 9) & (raw["date"].dt.hour < 16)]
    return raw[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def run_simulate(days: int = 20, capital: float = 50_000):
    """Simulate paper trading on recent historical data."""
    logger.info(f"Simulating paper trading on last {days} days of real data...")

    spot = fetch_spot_data(period=f"{days}d", interval="5m")
    if spot.empty:
        logger.error("No data fetched.")
        return

    logger.info(f"Data: {len(spot)} candles, {spot['date'].dt.date.nunique()} trading days")
    logger.info(f"Period: {spot['date'].iloc[0].date()} to {spot['date'].iloc[-1].date()}")

    traders = create_traders(capital)

    # Walk through each candle
    dates = spot["date"].tolist()
    for i, candle_time in enumerate(dates):
        if i < 50:  # need lookback
            continue

        window = spot.iloc[:i+1].copy()

        for vid, trader in traders.items():
            trader.scan(window, candle_time)

    # Print results
    print_comparison(traders)


def run_live(capital: float = 50_000, poll_seconds: int = 120):
    """Live paper trading — polls Yahoo Finance every 2 minutes."""
    logger.info("Starting LIVE paper trading (polls Yahoo every 2 min)...")
    logger.info(f"Capital per version: Rs.{capital:,.0f}")
    logger.info("Press Ctrl+C to stop.\n")

    traders = create_traders(capital)

    while True:
        now = datetime.now()

        # Only run during market hours (9:15 - 15:30 IST)
        if now.hour < 9 or (now.hour == 9 and now.minute < 15):
            logger.info("Market not open yet. Waiting...")
            time.sleep(60)
            continue
        if now.hour >= 16:
            logger.info("Market closed. Summary for today:")
            print_comparison(traders)
            logger.info("Waiting for next day...")
            time.sleep(3600)
            continue

        try:
            spot = fetch_spot_data(period="5d", interval="5m")
            if spot.empty:
                logger.warning("No data, retrying in 60s...")
                time.sleep(60)
                continue

            current_time = spot["date"].iloc[-1]
            spot_price = spot["close"].iloc[-1]
            logger.info(f"Tick: {current_time.strftime('%H:%M')} | NIFTY: {spot_price:.2f}")

            for vid, trader in traders.items():
                signal = trader.scan(spot, current_time)

            # Brief status
            for vid, trader in traders.items():
                s = trader.get_summary()
                status = "OPEN" if s["open_trade"] else "idle"
                logger.info(f"  [{vid[:6]}] P&L={s['pnl']:+,.0f} | "
                             f"Trades={s['trades']} WR={s['win_rate']}% | {status}")

        except Exception as e:
            logger.error(f"Error: {e}")

        time.sleep(poll_seconds)


def print_comparison(traders: dict):
    """Print side-by-side comparison."""
    print("\n" + "=" * 75)
    print("PAPER TRADING — 3-VERSION COMPARISON")
    print("=" * 75)

    summaries = {vid: t.get_summary() for vid, t in traders.items()}

    print(f"\n{'Metric':<20}", end="")
    for vid in summaries:
        print(f" {vid[:15]:>18}", end="")
    print()
    print("-" * 75)

    for metric in ["capital", "pnl", "roi", "trades", "winners", "losers", "win_rate"]:
        print(f"  {metric:<18}", end="")
        for vid, s in summaries.items():
            val = s[metric]
            if metric in ("capital", "pnl"):
                print(f" {val:>+18,.0f}", end="")
            elif metric == "roi":
                print(f" {val:>+17.1f}%", end="")
            elif metric == "win_rate":
                print(f" {val:>17.1f}%", end="")
            else:
                print(f" {val:>18}", end="")
        print()

    # Trade details per version
    for vid, trader in traders.items():
        closed = [t for t in trader.trades if t["status"] == "CLOSED"]
        if not closed:
            continue
        print(f"\n  {vid}:")
        for t in closed[-10:]:  # last 10 trades
            print(f"    #{t['id']} {t['entry_time'][:16]} {t['symbol']:>16} "
                  f"P&L={t['pnl']:>+8,.0f} {t['exit_reason']:>14}")

    print()


def generate_dashboard(traders: dict = None):
    """Generate HTML comparison dashboard."""
    if traders is None:
        traders = {}
        for vid, cfg in VERSION_CONFIG.items():
            path = TRADES_DIR / f"{vid}.json"
            if path.exists():
                with open(path) as f:
                    state = json.load(f)
                traders[vid] = state

    if not traders:
        print("No paper trading data found. Run --simulate or --live first.")
        return

    # Build HTML
    import json as json_mod
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = f"data/reports/paper_trading_{ts}.html"
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    versions_data = {}
    for vid, state in traders.items():
        if isinstance(state, PaperTrader):
            versions_data[vid] = {
                "name": VERSION_CONFIG[vid]["name"],
                "color": VERSION_CONFIG[vid]["color"],
                "capital": state.capital,
                "initial": state.initial_capital,
                "trades": state.trades,
            }
        else:
            versions_data[vid] = {
                "name": VERSION_CONFIG[vid]["name"],
                "color": VERSION_CONFIG[vid]["color"],
                "capital": state.get("capital", 50000),
                "initial": state.get("initial_capital", 50000),
                "trades": state.get("trades", []),
            }

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Paper Trading Comparison</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0e1a;color:#e0e0e0;padding:24px;}}
h1{{text-align:center;font-size:1.8em;background:linear-gradient(135deg,#00d4ff,#7b2ff7);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:20px;}}
.grid-3{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;}}
.card{{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:18px;}}
.card h3{{font-size:1em;margin-bottom:10px;}}
.kpi{{text-align:center;padding:8px;}} .kpi .v{{font-size:1.4em;font-weight:800;}} .kpi .l{{font-size:0.7em;color:#6b7280;}}
.chart-box{{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:16px;margin:14px 0;}}
table{{width:100%;border-collapse:collapse;font-size:0.82em;}} th{{background:#1a2240;color:#00d4ff;padding:8px;text-align:left;}}
td{{padding:7px 8px;border-bottom:1px solid #111827;}} tr:hover td{{background:#131830;}}
.pos{{color:#10b981;font-weight:600;}} .neg{{color:#ef4444;font-weight:600;}}
</style></head><body>
<h1>Paper Trading — 3-Version Comparison</h1>
<p style="text-align:center;color:#6b7280;margin-bottom:20px;">Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} | Capital: Rs.50,000 per version</p>
<div class="grid-3" id="cards"></div>
<div class="chart-box"><h3 style="color:#00d4ff;">Equity Curves</h3><canvas id="eqChart" height="250"></canvas></div>
<div class="chart-box"><h3 style="color:#00d4ff;">Trade Log</h3><div id="tradeLog"></div></div>
<script>
const V={json_mod.dumps(versions_data, default=str)};
const cards=document.getElementById('cards');
const eqLabels=[];const eqDatasets=[];
for(const[vid,v]of Object.entries(V)){{
  const trades=v.trades.filter(t=>t.status==='CLOSED');
  const wins=trades.filter(t=>t.pnl>0);
  const pnl=v.capital-v.initial;
  const roi=(pnl/v.initial*100).toFixed(1);
  const wr=trades.length?(wins.length/trades.length*100).toFixed(0):'0';
  const cls=pnl>=0?'pos':'neg';
  cards.innerHTML+=`<div class="card" style="border-top:3px solid ${{v.color}}">
    <h3 style="color:${{v.color}}">${{v.name}}</h3>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;">
      <div class="kpi"><div class="v ${{cls}}">${{pnl>=0?'+':''}}${{Math.round(pnl).toLocaleString()}}</div><div class="l">P&L</div></div>
      <div class="kpi"><div class="v ${{cls}}">${{roi}}%</div><div class="l">ROI</div></div>
      <div class="kpi"><div class="v">${{trades.length}}</div><div class="l">Trades</div></div>
      <div class="kpi"><div class="v">${{wr}}%</div><div class="l">Win Rate</div></div>
    </div></div>`;
  // Equity curve
  let eq=[v.initial];
  trades.forEach(t=>eq.push(eq[eq.length-1]+t.pnl));
  eqDatasets.push({{label:v.name,data:eq,borderColor:v.color,borderWidth:2,pointRadius:1,fill:false}});
}}
// Chart
const maxLen=Math.max(...eqDatasets.map(d=>d.data.length));
new Chart(document.getElementById('eqChart'),{{type:'line',data:{{
  labels:Array.from({{length:maxLen}},(_,i)=>i),datasets:eqDatasets}},
  options:{{responsive:true,plugins:{{legend:{{labels:{{color:'#9ca3af'}}}}}},
    scales:{{x:{{display:false}},y:{{ticks:{{color:'#6b7280'}},grid:{{color:'#1a2040'}}}}}}}}}});
// Trade log
let logH='<table><tr><th>Version</th><th>#</th><th>Date</th><th>Symbol</th><th>P&L</th><th>Exit</th></tr>';
for(const[vid,v]of Object.entries(V)){{
  v.trades.filter(t=>t.status==='CLOSED').forEach(t=>{{
    const cls=t.pnl>=0?'pos':'neg';
    logH+=`<tr><td style="color:${{v.color}}">${{v.name.substring(0,8)}}</td><td>${{t.id}}</td><td>${{(t.entry_time||'').substring(0,16)}}</td><td>${{t.symbol}}</td><td class="${{cls}}">${{t.pnl>=0?'+':''}}${{Math.round(t.pnl).toLocaleString()}}</td><td>${{t.exit_reason}}</td></tr>`;
  }});
}}
logH+='</table>';
document.getElementById('tradeLog').innerHTML=logH;
</script></body></html>"""

    with open(output, "w") as f:
        f.write(html)
    print(f"Dashboard saved: {output}")
    return output


def main():
    parser = argparse.ArgumentParser(description="Paper Trading — 3 versions")
    parser.add_argument("--live", action="store_true", help="Live mode (polls Yahoo)")
    parser.add_argument("--simulate", action="store_true", help="Simulate on recent data")
    parser.add_argument("--days", type=int, default=20, help="Days for simulation")
    parser.add_argument("--capital", type=float, default=50_000, help="Capital per version")
    parser.add_argument("--status", action="store_true", help="Show current status")
    parser.add_argument("--dashboard", action="store_true", help="Generate comparison dashboard")
    parser.add_argument("--reset", action="store_true", help="Reset all paper trades")

    args = parser.parse_args()

    if args.reset:
        for f in TRADES_DIR.glob("*.json"):
            f.unlink()
        print("Paper trades reset.")
        return

    if args.status or args.dashboard:
        traders = {}
        for vid, cfg in VERSION_CONFIG.items():
            path = TRADES_DIR / f"{vid}.json"
            if path.exists():
                detect_fn = cfg["detect_trend"]()
                traders[vid] = PaperTrader(vid, detect_fn, capital=args.capital)
        if traders:
            print_comparison(traders)
            if args.dashboard:
                path = generate_dashboard(traders)
                if path:
                    import subprocess
                    subprocess.run(["open", path], check=False)
        else:
            print("No paper trading data. Run --simulate or --live first.")
        return

    if args.simulate:
        run_simulate(args.days, args.capital)
        traders = create_traders(args.capital)
        path = generate_dashboard(traders)
        if path:
            import subprocess
            subprocess.run(["open", path], check=False)
        return

    if args.live:
        run_live(args.capital)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
