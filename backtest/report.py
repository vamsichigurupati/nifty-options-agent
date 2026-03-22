"""Generate interactive HTML report from backtest results."""

import json
from datetime import datetime
from pathlib import Path
from backtest.engine import BacktestResult
from backtest.analyzer import PerformanceMetrics


def generate_report(result: BacktestResult, metrics: PerformanceMetrics,
                    output_path: str = None) -> str:
    """Generate an HTML backtest report and return the file path."""
    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"data/reports/backtest_{ts}.html"

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Prepare chart data
    equity_dates = [e["date"].isoformat() if hasattr(e["date"], 'isoformat')
                    else str(e["date"]) for e in result.equity_curve]
    equity_values = [round(e["equity"], 2) for e in result.equity_curve]

    # Sample equity curve if too many points (for chart performance)
    if len(equity_dates) > 2000:
        step = len(equity_dates) // 2000
        equity_dates = equity_dates[::step]
        equity_values = equity_values[::step]

    daily_dates = [d["date"] for d in result.daily_pnl]
    daily_pnls = [round(d["pnl"], 2) for d in result.daily_pnl]
    daily_colors = ["#10b981" if p >= 0 else "#ef4444" for p in daily_pnls]

    # Cumulative daily PnL
    cum_daily = []
    running = 0
    for p in daily_pnls:
        running += p
        cum_daily.append(round(running, 2))

    # Trade list for table
    trade_rows = []
    for t in result.trades:
        trade_rows.append({
            "id": t.trade_id,
            "symbol": t.tradingsymbol,
            "strategy": t.strategy,
            "action": t.action,
            "entry": t.entry_price,
            "exit": t.exit_price,
            "qty": t.quantity,
            "pnl": round(t.net_pnl, 2),
            "charges": t.charges,
            "reason": t.exit_reason,
            "entry_time": t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "",
            "exit_time": t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else "",
            "holding": round((t.exit_time - t.entry_time).total_seconds() / 60, 0) if t.exit_time else 0,
            "mfe": round(t.max_favorable, 2),
            "mae": round(t.max_adverse, 2),
        })

    # PnL distribution
    pnl_values = [round(t.net_pnl, 2) for t in result.trades]

    # Exit reasons
    exit_labels = list(metrics.exit_reasons.keys())
    exit_counts = list(metrics.exit_reasons.values())

    # Strategy breakdown
    strat_labels = list(metrics.strategy_stats.keys())
    strat_pnls = [round(v["net_pnl"], 2) for v in metrics.strategy_stats.values()]
    strat_trades = [v["trades"] for v in metrics.strategy_stats.values()]
    strat_wr = [round(v["win_rate"], 1) for v in metrics.strategy_stats.values()]

    m = metrics  # shorthand

    # Pre-build dynamic sections (avoid nested f-string issues in Python 3.9)
    strategy_cards = ""
    for s, v in m.strategy_stats.items():
        pnl_class = "pnl-pos" if v["net_pnl"] >= 0 else "pnl-neg"
        strategy_cards += (
            f'<div class="strat-card">'
            f'<div class="strat-name">{s.upper()}</div>'
            f'<div style="font-size:0.88em;">'
            f'Trades: {v["trades"]} | Win Rate: {v["win_rate"]:.1f}% | '
            f'Net P&L: <span class="{pnl_class}">{v["net_pnl"]:+,.0f}</span> | '
            f'Avg: {v["avg_pnl"]:+,.0f}/trade'
            f'</div></div>\n'
        )

    trade_log_rows = ""
    for t in trade_rows:
        pnl_class = "pnl-pos" if t["pnl"] >= 0 else "pnl-neg"
        trade_log_rows += (
            f'<tr><td>{t["id"]}</td><td>{t["symbol"][:25]}</td>'
            f'<td>{t["strategy"]}</td><td>{t["entry"]:.2f}</td>'
            f'<td>{t["exit"]:.2f}</td><td>{t["qty"]}</td>'
            f'<td class="{pnl_class}">{t["pnl"]:+,.2f}</td>'
            f'<td>{t["charges"]:.2f}</td><td>{t["reason"]}</td>'
            f'<td>{t["entry_time"]}</td><td>{t["holding"]:.0f}m</td></tr>\n'
        )

    param_items = ""
    for k, v in result.params.items():
        param_items += f'<div class="param"><span class="pk">{k}</span><span class="pv">{v}</span></div>\n'

    sharpe_class = "green" if m.sharpe_ratio > 1 else ("orange" if m.sharpe_ratio > 0 else "red")
    pf_class = "green" if m.profit_factor > 1.5 else ("orange" if m.profit_factor > 1 else "red")
    pnl_color = "#10b981" if m.net_pnl >= 0 else "#ef4444"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backtest Report — {datetime.now().strftime("%Y-%m-%d %H:%M")}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:'Segoe UI',system-ui,sans-serif; background:#0a0e1a; color:#e0e0e0; padding:24px; }}
  h1 {{ text-align:center; font-size:2em; margin-bottom:6px;
       background:linear-gradient(135deg,#00d4ff,#7b2ff7);
       -webkit-background-clip:text; -webkit-text-fill-color:transparent; }}
  .subtitle {{ text-align:center; color:#666; margin-bottom:30px; }}
  h2 {{ color:#00d4ff; font-size:1.3em; margin:30px 0 14px; border-bottom:1px solid #1a2040; padding-bottom:8px; }}

  .kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:16px 0; }}
  .kpi {{ background:#111827; border:1px solid #1f2937; border-radius:12px; padding:16px; text-align:center; }}
  .kpi .value {{ font-size:1.6em; font-weight:800; }}
  .kpi .label {{ font-size:0.78em; color:#9ca3af; margin-top:4px; }}
  .kpi.green .value {{ color:#10b981; }}
  .kpi.red .value {{ color:#ef4444; }}
  .kpi.blue .value {{ color:#3b82f6; }}
  .kpi.purple .value {{ color:#a78bfa; }}
  .kpi.orange .value {{ color:#f59e0b; }}
  .kpi.auto .value {{ color: {('#10b981' if m.net_pnl >= 0 else '#ef4444')}; }}

  .chart-container {{ background:#111827; border:1px solid #1f2937; border-radius:12px; padding:20px; margin:16px 0; }}
  .chart-row {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
  @media(max-width:800px) {{ .chart-row {{ grid-template-columns:1fr; }} }}

  table {{ width:100%; border-collapse:collapse; margin:12px 0; font-size:0.85em; }}
  th {{ background:#1a2240; color:#00d4ff; padding:10px 8px; text-align:left; position:sticky; top:0; }}
  td {{ padding:8px; border-bottom:1px solid #1a2040; }}
  tr:hover td {{ background:#131830; }}
  .pnl-pos {{ color:#10b981; font-weight:600; }}
  .pnl-neg {{ color:#ef4444; font-weight:600; }}

  .table-wrap {{ max-height:500px; overflow-y:auto; border:1px solid #1f2937; border-radius:12px; }}

  .strat-card {{ background:#0f1629; border:1px solid #253050; border-radius:10px; padding:16px; }}
  .strat-name {{ font-weight:700; color:#a78bfa; margin-bottom:8px; }}

  .param-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:8px; font-size:0.85em; }}
  .param {{ background:#111827; padding:8px 12px; border-radius:6px; display:flex; justify-content:space-between; }}
  .param .pk {{ color:#9ca3af; }} .param .pv {{ color:#fff; font-weight:600; }}
</style>
</head>
<body>

<h1>Backtest Report</h1>
<p class="subtitle">Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} | {m.trading_days} trading days | {m.total_trades} trades</p>

<!-- ══ KPI Overview ══ -->
<h2>Performance Overview</h2>
<div class="kpi-grid">
  <div class="kpi auto" style="--auto-color:{pnl_color};"><div class="value" style="color:{pnl_color};">{m.net_pnl:+,.0f}</div><div class="label">Net P&L (INR)</div></div>
  <div class="kpi auto"><div class="value" style="color:{pnl_color};">{m.net_return_pct:+.1f}%</div><div class="label">Return on Capital</div></div>
  <div class="kpi {sharpe_class}"><div class="value">{m.sharpe_ratio}</div><div class="label">Sharpe Ratio</div></div>
  <div class="kpi {pf_class}"><div class="value">{m.profit_factor}</div><div class="label">Profit Factor</div></div>
  <div class="kpi red"><div class="value">{m.max_drawdown:,.0f}</div><div class="label">Max Drawdown (INR)</div></div>
  <div class="kpi blue"><div class="value">{m.win_rate:.1f}%</div><div class="label">Win Rate</div></div>
  <div class="kpi purple"><div class="value">{m.total_trades}</div><div class="label">Total Trades</div></div>
  <div class="kpi orange"><div class="value">{m.annualized_return:+.1f}%</div><div class="label">Annualized Return</div></div>
</div>

<div class="kpi-grid">
  <div class="kpi green"><div class="value">{m.avg_winner:+,.0f}</div><div class="label">Avg Winner</div></div>
  <div class="kpi red"><div class="value">{m.avg_loser:,.0f}</div><div class="label">Avg Loser</div></div>
  <div class="kpi green"><div class="value">{m.largest_winner:+,.0f}</div><div class="label">Largest Winner</div></div>
  <div class="kpi red"><div class="value">{m.largest_loser:,.0f}</div><div class="label">Largest Loser</div></div>
  <div class="kpi blue"><div class="value">{m.expectancy:+,.0f}</div><div class="label">Expectancy (INR/trade)</div></div>
  <div class="kpi purple"><div class="value">{m.sortino_ratio}</div><div class="label">Sortino Ratio</div></div>
  <div class="kpi orange"><div class="value">{m.calmar_ratio:.2f}</div><div class="label">Calmar Ratio</div></div>
  <div class="kpi blue"><div class="value">{m.total_charges:,.0f}</div><div class="label">Total Charges</div></div>
</div>

<!-- ══ Equity Curve ══ -->
<h2>Equity Curve</h2>
<div class="chart-container" style="height:350px;">
  <canvas id="equityChart"></canvas>
</div>

<!-- ══ Daily P&L ══ -->
<h2>Daily P&L</h2>
<div class="chart-row">
  <div class="chart-container" style="height:300px;">
    <canvas id="dailyPnlChart"></canvas>
  </div>
  <div class="chart-container" style="height:300px;">
    <canvas id="cumPnlChart"></canvas>
  </div>
</div>

<div class="kpi-grid" style="margin-top:12px;">
  <div class="kpi green"><div class="value">{m.profitable_days}</div><div class="label">Profitable Days</div></div>
  <div class="kpi red"><div class="value">{m.losing_days}</div><div class="label">Losing Days</div></div>
  <div class="kpi green"><div class="value">{m.best_day:+,.0f}</div><div class="label">Best Day</div></div>
  <div class="kpi red"><div class="value">{m.worst_day:,.0f}</div><div class="label">Worst Day</div></div>
  <div class="kpi blue"><div class="value">{m.avg_daily_pnl:+,.0f}</div><div class="label">Avg Daily P&L</div></div>
  <div class="kpi purple"><div class="value">{m.max_drawdown_pct:.1f}%</div><div class="label">Max Drawdown %</div></div>
</div>

<!-- ══ P&L Distribution + Exit Reasons ══ -->
<h2>Trade Analysis</h2>
<div class="chart-row">
  <div class="chart-container" style="height:300px;">
    <canvas id="pnlDistChart"></canvas>
  </div>
  <div class="chart-container" style="height:300px;">
    <canvas id="exitChart"></canvas>
  </div>
</div>

<div class="kpi-grid" style="margin-top:12px;">
  <div class="kpi green"><div class="value">{m.max_consecutive_wins}</div><div class="label">Max Consecutive Wins</div></div>
  <div class="kpi red"><div class="value">{m.max_consecutive_losses}</div><div class="label">Max Consecutive Losses</div></div>
  <div class="kpi blue"><div class="value">{m.avg_holding_minutes:.0f} min</div><div class="label">Avg Holding Time</div></div>
  <div class="kpi green"><div class="value">{m.avg_winner_holding:.0f} min</div><div class="label">Avg Winner Holding</div></div>
  <div class="kpi red"><div class="value">{m.avg_loser_holding:.0f} min</div><div class="label">Avg Loser Holding</div></div>
  <div class="kpi purple"><div class="value">{m.avg_mfe:+,.0f}</div><div class="label">Avg Max Favorable (MFE)</div></div>
  <div class="kpi orange"><div class="value">{m.avg_mae:,.0f}</div><div class="label">Avg Max Adverse (MAE)</div></div>
</div>

<!-- ══ Strategy Breakdown ══ -->
<h2>Strategy Breakdown</h2>
<div class="chart-row">
  {strategy_cards}
</div>

<!-- ══ Trade Log ══ -->
<h2>Trade Log ({m.total_trades} trades)</h2>
<div class="table-wrap">
<table>
<tr><th>#</th><th>Symbol</th><th>Strategy</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&L</th><th>Charges</th><th>Exit Reason</th><th>Entry Time</th><th>Holding</th></tr>
{trade_log_rows}
</table>
</div>

<!-- ══ Parameters ══ -->
<h2>Backtest Parameters</h2>
<div class="param-grid">
{param_items}
</div>

<script>
const chartDefaults = {{
  responsive: true, maintainAspectRatio: false,
  plugins: {{ legend: {{ labels: {{ color: '#9ca3af' }} }} }},
  scales: {{
    x: {{ ticks: {{ color: '#6b7280', maxTicksLimit: 15 }}, grid: {{ color: '#1a2040' }} }},
    y: {{ ticks: {{ color: '#6b7280' }}, grid: {{ color: '#1a2040' }} }}
  }}
}};

// Equity Curve
new Chart(document.getElementById('equityChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(equity_dates)},
    datasets: [{{ label: 'Equity', data: {json.dumps(equity_values)},
      borderColor: '#00d4ff', backgroundColor: 'rgba(0,212,255,0.1)',
      fill: true, pointRadius: 0, borderWidth: 2 }}]
  }},
  options: {{ ...chartDefaults, plugins: {{ ...chartDefaults.plugins, title: {{ display: true, text: 'Equity Curve', color: '#e0e0e0' }} }} }}
}});

// Daily P&L Bars
new Chart(document.getElementById('dailyPnlChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(daily_dates)},
    datasets: [{{ label: 'Daily P&L', data: {json.dumps(daily_pnls)},
      backgroundColor: {json.dumps(daily_colors)} }}]
  }},
  options: {{ ...chartDefaults, plugins: {{ ...chartDefaults.plugins, title: {{ display: true, text: 'Daily P&L', color: '#e0e0e0' }} }} }}
}});

// Cumulative Daily P&L
new Chart(document.getElementById('cumPnlChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(daily_dates)},
    datasets: [{{ label: 'Cumulative P&L', data: {json.dumps(cum_daily)},
      borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,0.1)',
      fill: true, pointRadius: 2, borderWidth: 2 }}]
  }},
  options: {{ ...chartDefaults, plugins: {{ ...chartDefaults.plugins, title: {{ display: true, text: 'Cumulative P&L', color: '#e0e0e0' }} }} }}
}});

// P&L Distribution
new Chart(document.getElementById('pnlDistChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(list(range(len(pnl_values))))},
    datasets: [{{ label: 'Trade P&L', data: {json.dumps(pnl_values)},
      backgroundColor: {json.dumps(["#10b981" if p>=0 else "#ef4444" for p in pnl_values])} }}]
  }},
  options: {{ ...chartDefaults, plugins: {{ ...chartDefaults.plugins, title: {{ display: true, text: 'P&L Distribution (per trade)', color: '#e0e0e0' }} }},
    scales: {{ ...chartDefaults.scales, x: {{ display: false }} }} }}
}});

// Exit Reasons Pie
new Chart(document.getElementById('exitChart'), {{
  type: 'doughnut',
  data: {{
    labels: {json.dumps(exit_labels)},
    datasets: [{{ data: {json.dumps(exit_counts)},
      backgroundColor: ['#ef4444','#10b981','#f59e0b','#3b82f6','#a78bfa','#06b6d4','#ec4899'] }}]
  }},
  options: {{ responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'right', labels: {{ color: '#9ca3af' }} }},
              title: {{ display: true, text: 'Exit Reasons', color: '#e0e0e0' }} }} }}
}});
</script>
</body>
</html>"""

    with open(output_path, "w") as f:
        f.write(html)

    return output_path
