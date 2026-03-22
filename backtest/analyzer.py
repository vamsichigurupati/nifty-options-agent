"""Performance analytics — compute all metrics from backtest results."""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from backtest.engine import BacktestResult


@dataclass
class PerformanceMetrics:
    # ── Overview ──
    total_trades: int
    winners: int
    losers: int
    win_rate: float          # %
    avg_winner: float        # INR
    avg_loser: float         # INR
    largest_winner: float
    largest_loser: float
    avg_trade_pnl: float

    # ── Returns ──
    gross_pnl: float
    total_charges: float
    net_pnl: float
    net_return_pct: float    # % on initial capital
    annualized_return: float # %

    # ── Risk ──
    max_drawdown: float      # INR
    max_drawdown_pct: float  # %
    max_drawdown_duration: int  # trading days
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    profit_factor: float     # gross profit / gross loss

    # ── Trade Analysis ──
    avg_holding_minutes: float
    avg_winner_holding: float
    avg_loser_holding: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    expectancy: float        # avg win * win_rate - avg loss * loss_rate

    # ── Strategy Breakdown ──
    strategy_stats: dict     # per-strategy metrics

    # ── Exit Reason Breakdown ──
    exit_reasons: dict       # reason -> count

    # ── Daily Stats ──
    trading_days: int
    profitable_days: int
    losing_days: int
    best_day: float
    worst_day: float
    avg_daily_pnl: float

    # ── MFE/MAE ──
    avg_mfe: float           # avg max favorable excursion
    avg_mae: float           # avg max adverse excursion


def analyze(result: BacktestResult, initial_capital: float = 100_000) -> PerformanceMetrics:
    """Compute comprehensive performance metrics from backtest results."""
    trades = result.trades
    if not trades:
        return _empty_metrics()

    # ── Basic Stats ──
    pnls = [t.net_pnl for t in trades]
    winners = [t for t in trades if t.net_pnl > 0]
    losers = [t for t in trades if t.net_pnl <= 0]

    win_pnls = [t.net_pnl for t in winners]
    loss_pnls = [t.net_pnl for t in losers]

    total = len(trades)
    n_win = len(winners)
    n_lose = len(losers)
    win_rate = (n_win / total * 100) if total > 0 else 0

    gross_pnl = sum(t.pnl for t in trades)
    total_charges = sum(t.charges for t in trades)
    net_pnl = sum(pnls)
    net_return = (net_pnl / initial_capital * 100) if initial_capital > 0 else 0

    # ── Holding Times ──
    def holding_mins(t):
        if t.exit_time and t.entry_time:
            return (t.exit_time - t.entry_time).total_seconds() / 60
        return 0

    all_holdings = [holding_mins(t) for t in trades]
    win_holdings = [holding_mins(t) for t in winners]
    lose_holdings = [holding_mins(t) for t in losers]

    # ── Equity Curve & Drawdown ──
    equity = [initial_capital]
    for p in pnls:
        equity.append(equity[-1] + p)
    equity_arr = np.array(equity)

    peak = np.maximum.accumulate(equity_arr)
    drawdown = equity_arr - peak
    max_dd = float(np.min(drawdown))
    max_dd_pct = float(np.min(drawdown / peak) * 100) if peak.max() > 0 else 0

    # Drawdown duration (in trades, approximate)
    dd_duration = 0
    max_dd_duration = 0
    for i in range(len(drawdown)):
        if drawdown[i] < 0:
            dd_duration += 1
            max_dd_duration = max(max_dd_duration, dd_duration)
        else:
            dd_duration = 0

    # ── Daily P&L ──
    daily = result.daily_pnl
    daily_pnls = [d["pnl"] for d in daily]
    trading_days = len(daily)
    profitable_days = sum(1 for p in daily_pnls if p > 0)
    losing_days = sum(1 for p in daily_pnls if p <= 0)

    # ── Sharpe, Sortino, Calmar ──
    if len(daily_pnls) > 1:
        daily_returns = np.array(daily_pnls)
        mean_daily = np.mean(daily_returns)
        std_daily = np.std(daily_returns, ddof=1)

        # Sharpe (annualized, 252 trading days)
        sharpe = (mean_daily / std_daily * np.sqrt(252)) if std_daily > 0 else 0

        # Sortino (only downside deviation)
        downside = daily_returns[daily_returns < 0]
        downside_std = np.std(downside, ddof=1) if len(downside) > 1 else 1
        sortino = (mean_daily / downside_std * np.sqrt(252)) if downside_std > 0 else 0

        # Calmar (annualized return / max drawdown)
        ann_return = mean_daily * 252
        calmar = abs(ann_return / max_dd) if max_dd != 0 else 0
        ann_return_pct = (ann_return / initial_capital * 100) if initial_capital > 0 else 0
    else:
        sharpe = sortino = calmar = ann_return_pct = 0
        mean_daily = daily_pnls[0] if daily_pnls else 0

    # ── Profit Factor ──
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

    # ── Consecutive Wins/Losses ──
    max_consec_wins = _max_consecutive(pnls, lambda x: x > 0)
    max_consec_losses = _max_consecutive(pnls, lambda x: x <= 0)

    # ── Expectancy ──
    avg_win = np.mean(win_pnls) if win_pnls else 0
    avg_loss = abs(np.mean(loss_pnls)) if loss_pnls else 0
    loss_rate = n_lose / total if total > 0 else 0
    expectancy = (avg_win * win_rate / 100) - (avg_loss * loss_rate)

    # ── Strategy Breakdown ──
    strategy_stats = {}
    strat_groups = {}
    for t in trades:
        strat_groups.setdefault(t.strategy, []).append(t)
    for strat, strat_trades in strat_groups.items():
        s_pnls = [t.net_pnl for t in strat_trades]
        s_wins = sum(1 for p in s_pnls if p > 0)
        strategy_stats[strat] = {
            "trades": len(strat_trades),
            "winners": s_wins,
            "losers": len(strat_trades) - s_wins,
            "win_rate": s_wins / len(strat_trades) * 100 if strat_trades else 0,
            "net_pnl": sum(s_pnls),
            "avg_pnl": np.mean(s_pnls),
        }

    # ── Exit Reason Breakdown ──
    exit_reasons = {}
    for t in trades:
        r = t.exit_reason
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # ── MFE/MAE ──
    avg_mfe = np.mean([t.max_favorable for t in trades])
    avg_mae = np.mean([t.max_adverse for t in trades])

    return PerformanceMetrics(
        total_trades=total,
        winners=n_win,
        losers=n_lose,
        win_rate=round(win_rate, 1),
        avg_winner=round(float(avg_win), 2),
        avg_loser=round(float(-avg_loss), 2),
        largest_winner=round(max(pnls), 2) if pnls else 0,
        largest_loser=round(min(pnls), 2) if pnls else 0,
        avg_trade_pnl=round(float(np.mean(pnls)), 2),
        gross_pnl=round(gross_pnl, 2),
        total_charges=round(total_charges, 2),
        net_pnl=round(net_pnl, 2),
        net_return_pct=round(net_return, 2),
        annualized_return=round(ann_return_pct, 2),
        max_drawdown=round(max_dd, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        max_drawdown_duration=max_dd_duration,
        sharpe_ratio=round(float(sharpe), 2),
        sortino_ratio=round(float(sortino), 2),
        calmar_ratio=round(float(calmar), 2),
        profit_factor=round(profit_factor, 2),
        avg_holding_minutes=round(float(np.mean(all_holdings)), 1) if all_holdings else 0,
        avg_winner_holding=round(float(np.mean(win_holdings)), 1) if win_holdings else 0,
        avg_loser_holding=round(float(np.mean(lose_holdings)), 1) if lose_holdings else 0,
        max_consecutive_wins=max_consec_wins,
        max_consecutive_losses=max_consec_losses,
        expectancy=round(expectancy, 2),
        strategy_stats=strategy_stats,
        exit_reasons=exit_reasons,
        trading_days=trading_days,
        profitable_days=profitable_days,
        losing_days=losing_days,
        best_day=round(max(daily_pnls), 2) if daily_pnls else 0,
        worst_day=round(min(daily_pnls), 2) if daily_pnls else 0,
        avg_daily_pnl=round(float(mean_daily), 2),
        avg_mfe=round(float(avg_mfe), 2),
        avg_mae=round(float(avg_mae), 2),
    )


def _max_consecutive(values, condition):
    max_c = 0
    current = 0
    for v in values:
        if condition(v):
            current += 1
            max_c = max(max_c, current)
        else:
            current = 0
    return max_c


def _empty_metrics():
    return PerformanceMetrics(
        total_trades=0, winners=0, losers=0, win_rate=0,
        avg_winner=0, avg_loser=0, largest_winner=0, largest_loser=0,
        avg_trade_pnl=0, gross_pnl=0, total_charges=0, net_pnl=0,
        net_return_pct=0, annualized_return=0, max_drawdown=0,
        max_drawdown_pct=0, max_drawdown_duration=0, sharpe_ratio=0,
        sortino_ratio=0, calmar_ratio=0, profit_factor=0,
        avg_holding_minutes=0, avg_winner_holding=0, avg_loser_holding=0,
        max_consecutive_wins=0, max_consecutive_losses=0, expectancy=0,
        strategy_stats={}, exit_reasons={}, trading_days=0,
        profitable_days=0, losing_days=0, best_day=0, worst_day=0,
        avg_daily_pnl=0, avg_mfe=0, avg_mae=0,
    )
