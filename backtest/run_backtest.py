#!/usr/bin/env python3
"""
Run a backtest of the trading strategies.

Usage:
  # Quick test with synthetic data (no Kite API needed):
  python -m backtest.run_backtest --synthetic

  # With real Kite historical data:
  python -m backtest.run_backtest --from 2025-01-01 --to 2025-03-01 --underlying NIFTY

  # Custom parameters:
  python -m backtest.run_backtest --synthetic --capital 200000 --sl-pct 0.25 --rr 2.5

  # Specific strategy only:
  python -m backtest.run_backtest --synthetic --strategy momentum

  # Parameter sweep (find best SL %):
  python -m backtest.run_backtest --synthetic --sweep sl_pct 0.15 0.40 0.05
"""

import argparse
import logging
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.engine import BacktestEngine
from backtest.analyzer import analyze
from backtest.report import generate_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_single(args) -> dict:
    """Run a single backtest and return metrics."""
    params = {}
    if args.sl_pct:
        params["sl_pct"] = args.sl_pct
    if args.rr:
        params["rr_ratio"] = args.rr
    if args.max_loss_trade:
        params["max_loss_per_trade"] = args.max_loss_trade
    if args.max_loss_day:
        params["max_loss_per_day"] = args.max_loss_day

    strategies = [args.strategy] if args.strategy else None

    # Multi-underlying mode
    if args.underlying == "BOTH":
        return _run_multi(args, params, strategies)

    if args.synthetic:
        from backtest.data_fetcher import generate_synthetic_data
        interval = args.interval
        logger.info(f"Generating synthetic data ({args.days} days, {args.underlying}, {interval}-min candles)...")
        data = generate_synthetic_data(
            underlying=args.underlying,
            days=args.days,
            spot_start=args.spot_start,
            interval_minutes=interval,
        )
        spot_df = data["spot"]
        options = data["options"]
    else:
        # Use Kite historical data
        from broker.kite_auth import KiteAuth
        from backtest.data_fetcher import HistoricalDataFetcher

        logger.info("Logging into Kite Connect...")
        auth = KiteAuth()
        kite = auth.get_kite()
        fetcher = HistoricalDataFetcher(kite)

        logger.info(f"Fetching spot data {args.from_date} to {args.to_date}...")
        spot_df = fetcher.get_spot_history(
            args.underlying, args.from_date, args.to_date, "5minute"
        )
        if spot_df.empty:
            logger.error("No spot data returned. Check dates and API connection.")
            sys.exit(1)

        # Get nearest expiry options
        import pandas as pd
        from datetime import datetime
        instruments = kite.instruments("NFO")
        prefix = args.underlying
        from config.instruments import UNDERLYING_MAP
        nfo_prefix = UNDERLYING_MAP[args.underlying]["nfo_prefix"]
        strike_step = UNDERLYING_MAP[args.underlying]["strike_step"]

        # Get unique expiries in range
        from datetime import date
        start = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        end = datetime.strptime(args.to_date, "%Y-%m-%d").date()

        opt_instruments = [i for i in instruments
                           if i["name"] == nfo_prefix
                           and i["instrument_type"] in ("CE", "PE")
                           and i["segment"] == "NFO-OPT"
                           and start <= i["expiry"] <= end + pd.Timedelta(days=7)]

        # Get ATM strikes based on first spot price
        first_spot = spot_df.iloc[0]["close"]
        atm = round(first_spot / strike_step) * strike_step
        target_strikes = [atm + i * strike_step for i in range(-5, 6)]
        opt_instruments = [i for i in opt_instruments if i["strike"] in target_strikes]

        logger.info(f"Fetching {len(opt_instruments)} option histories...")
        options = {}
        for inst in opt_instruments:
            sym = inst["tradingsymbol"]
            df = fetcher.get_option_history(
                inst["instrument_token"], args.from_date, args.to_date, "5minute"
            )
            if not df.empty:
                df["strike"] = inst["strike"]
                df["instrument_type"] = inst["instrument_type"]
                df["tradingsymbol"] = sym
                df["lot_size"] = inst["lot_size"]
                options[sym] = df

        if not options:
            logger.error("No option data fetched.")
            sys.exit(1)

    logger.info(f"Running backtest: {len(spot_df)} candles, {len(options)} options...")
    engine = BacktestEngine(
        spot_data=spot_df,
        options_data=options,
        underlying=args.underlying,
        initial_capital=args.capital,
        strategies=strategies,
        params=params,
    )
    result = engine.run()
    metrics = analyze(result, args.capital)

    # Print summary
    _print_summary(metrics)

    # Generate report / dashboard
    if not args.no_report:
        if args.dashboard:
            from backtest.dashboard import generate_dashboard
            path = generate_dashboard(result, metrics)
            logger.info(f"Dashboard saved to: {path}")
        else:
            path = generate_report(result, metrics)
            logger.info(f"Report saved to: {path}")
        if not args.no_open:
            import subprocess
            subprocess.run(["open", path], check=False)

    return {"result": result, "metrics": metrics}


def _run_multi(args, params, strategies):
    """Run backtest across NIFTY + BANKNIFTY combined."""
    from backtest.engine import MultiBacktestEngine

    if args.synthetic:
        from backtest.data_fetcher import generate_multi_synthetic_data
        logger.info(f"Generating synthetic data ({args.days} days, NIFTY + BANKNIFTY)...")
        multi_data = generate_multi_synthetic_data(
            underlyings=["NIFTY", "BANKNIFTY"],
            days=args.days,
            interval_minutes=args.interval,
        )
    else:
        logger.error("Multi-underlying with real data not yet implemented. Use --synthetic.")
        sys.exit(1)

    logger.info("Running multi-underlying backtest...")
    engine = MultiBacktestEngine(
        multi_data=multi_data,
        initial_capital=args.capital,
        strategies=strategies,
        params=params,
    )
    result = engine.run()
    metrics = analyze(result, args.capital)

    _print_summary(metrics)

    # Per-underlying breakdown
    nifty_trades = [t for t in result.trades if t.underlying == "NIFTY"]
    bnf_trades = [t for t in result.trades if t.underlying == "BANKNIFTY"]
    print("\n  UNDERLYING BREAKDOWN:")
    for name, group in [("NIFTY", nifty_trades), ("BANKNIFTY", bnf_trades)]:
        if group:
            wins = sum(1 for t in group if t.net_pnl > 0)
            wr = wins / len(group) * 100
            pnl = sum(t.net_pnl for t in group)
            ce = sum(1 for t in group if "CE" in t.tradingsymbol)
            pe = len(group) - ce
            print(f"    {name}: {len(group)} trades (CE:{ce} PE:{pe}) | WR={wr:.0f}% | P&L={pnl:+,.0f}")

    if not args.no_report:
        if args.dashboard:
            from backtest.dashboard import generate_dashboard
            path = generate_dashboard(result, metrics, initial_capital=args.capital)
            logger.info(f"Dashboard saved to: {path}")
        else:
            path = generate_report(result, metrics)
            logger.info(f"Report saved to: {path}")
        if not args.no_open:
            import subprocess
            subprocess.run(["open", path], check=False)

    return {"result": result, "metrics": metrics}


def run_sweep(args):
    """Run a parameter sweep to find optimal values."""
    import numpy as np

    param_name = args.sweep[0]
    start = float(args.sweep[1])
    end = float(args.sweep[2])
    step = float(args.sweep[3])
    values = np.arange(start, end + step/2, step)

    logger.info(f"Parameter sweep: {param_name} from {start} to {end} step {step}")
    logger.info(f"Running {len(values)} backtests...\n")

    # Generate data once
    if args.synthetic:
        from backtest.data_fetcher import generate_synthetic_data
        data = generate_synthetic_data(
            underlying=args.underlying, days=args.days, spot_start=args.spot_start,
        )
        spot_df = data["spot"]
        options = data["options"]
    else:
        logger.error("Sweep only supported with --synthetic for now")
        sys.exit(1)

    results = []
    strategies = [args.strategy] if args.strategy else None

    for val in values:
        params = {param_name: round(float(val), 4)}
        engine = BacktestEngine(
            spot_data=spot_df, options_data=options,
            underlying=args.underlying, initial_capital=args.capital,
            strategies=strategies, params=params,
        )
        result = engine.run()
        m = analyze(result, args.capital)
        results.append({
            "value": round(float(val), 4),
            "net_pnl": m.net_pnl,
            "return_pct": m.net_return_pct,
            "sharpe": m.sharpe_ratio,
            "win_rate": m.win_rate,
            "trades": m.total_trades,
            "max_dd": m.max_drawdown,
            "profit_factor": m.profit_factor,
        })
        logger.info(
            f"  {param_name}={val:.4f} -> P&L={m.net_pnl:+,.0f} | "
            f"Sharpe={m.sharpe_ratio} | WR={m.win_rate}% | "
            f"Trades={m.total_trades} | PF={m.profit_factor}"
        )

    # Print sweep summary
    print("\n" + "=" * 80)
    print(f"PARAMETER SWEEP: {param_name}")
    print("=" * 80)
    print(f"{'Value':>10} {'Net P&L':>12} {'Return%':>10} {'Sharpe':>8} "
          f"{'WinRate':>8} {'Trades':>7} {'MaxDD':>10} {'PF':>8}")
    print("-" * 80)
    for r in results:
        print(f"{r['value']:>10.4f} {r['net_pnl']:>12,.0f} {r['return_pct']:>9.1f}% "
              f"{r['sharpe']:>8.2f} {r['win_rate']:>7.1f}% {r['trades']:>7} "
              f"{r['max_dd']:>10,.0f} {r['profit_factor']:>8.2f}")

    best = max(results, key=lambda r: r["sharpe"])
    print(f"\nBest by Sharpe: {param_name}={best['value']} "
          f"(Sharpe={best['sharpe']}, P&L={best['net_pnl']:+,.0f})")


def _print_summary(m):
    print("\n" + "=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Net P&L:         {'+' if m.net_pnl>=0 else ''}{m.net_pnl:>12,.2f}")
    print(f"  Return:          {m.net_return_pct:>11.1f}%")
    print(f"  Annualized:      {m.annualized_return:>11.1f}%")
    print(f"  Sharpe Ratio:    {m.sharpe_ratio:>11.2f}")
    print(f"  Sortino Ratio:   {m.sortino_ratio:>11.2f}")
    print(f"  Profit Factor:   {m.profit_factor:>11.2f}")
    print(f"  Max Drawdown:    {m.max_drawdown:>12,.2f} ({m.max_drawdown_pct:.1f}%)")
    print(f"  Win Rate:        {m.win_rate:>11.1f}% ({m.winners}W / {m.losers}L)")
    print(f"  Total Trades:    {m.total_trades:>11}")
    print(f"  Avg Trade P&L:   {m.avg_trade_pnl:>12,.2f}")
    print(f"  Avg Winner:      {m.avg_winner:>12,.2f}")
    print(f"  Avg Loser:       {m.avg_loser:>12,.2f}")
    print(f"  Expectancy:      {m.expectancy:>12,.2f}")
    print(f"  Total Charges:   {m.total_charges:>12,.2f}")
    print(f"  Avg Holding:     {m.avg_holding_minutes:>10.0f} min")
    print(f"  Consec Wins:     {m.max_consecutive_wins:>11}")
    print(f"  Consec Losses:   {m.max_consecutive_losses:>11}")
    print("-" * 60)
    print("  STRATEGY BREAKDOWN:")
    for s, v in m.strategy_stats.items():
        print(f"    {s}: {v['trades']} trades | WR={v['win_rate']:.0f}% | "
              f"P&L={v['net_pnl']:+,.0f}")
    print("-" * 60)
    print("  EXIT REASONS:")
    for reason, count in sorted(m.exit_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason}: {count}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Backtest trading strategies")

    # Data source
    parser.add_argument("--synthetic", action="store_true",
                        help="Use synthetic data (no Kite API needed)")
    parser.add_argument("--from", dest="from_date", default="2025-01-01",
                        help="Start date YYYY-MM-DD (for real data)")
    parser.add_argument("--to", dest="to_date", default="2025-03-01",
                        help="End date YYYY-MM-DD (for real data)")
    parser.add_argument("--days", type=int, default=30,
                        help="Days of synthetic data (default: 30)")
    parser.add_argument("--spot-start", type=float, default=22500,
                        help="Starting spot price for synthetic data")
    parser.add_argument("--interval", type=int, default=1,
                        help="Candle interval in minutes (default: 1)")

    # Trading params
    parser.add_argument("--underlying", default="NIFTY", choices=["NIFTY", "BANKNIFTY", "BOTH"])
    parser.add_argument("--capital", type=float, default=100_000,
                        help="Initial capital (default: 100000)")
    parser.add_argument("--strategy", choices=["momentum", "oi_reversal"],
                        help="Run only one strategy")
    parser.add_argument("--sl-pct", type=float, help="Stop loss %% (e.g., 0.30)")
    parser.add_argument("--rr", type=float, help="Risk:reward ratio (e.g., 2.0)")
    parser.add_argument("--max-loss-trade", type=float, help="Max loss per trade")
    parser.add_argument("--max-loss-day", type=float, help="Max daily loss")

    # Sweep
    parser.add_argument("--sweep", nargs=4,
                        metavar=("PARAM", "START", "END", "STEP"),
                        help="Sweep a parameter: --sweep sl_pct 0.15 0.40 0.05")

    # Output
    parser.add_argument("--dashboard", action="store_true",
                        help="Generate interactive dashboard instead of static report")
    parser.add_argument("--no-report", action="store_true",
                        help="Skip HTML report generation")
    parser.add_argument("--no-open", action="store_true",
                        help="Don't auto-open the report")

    args = parser.parse_args()

    if args.sweep:
        run_sweep(args)
    else:
        run_single(args)


if __name__ == "__main__":
    main()
