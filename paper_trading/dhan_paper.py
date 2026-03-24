#!/usr/bin/env python3
"""
Paper trading with Dhan — uses the ACTUAL backtest engine for signal generation.

Same code that produced +35K in backtesting now runs live.
Dhan API provides real spot data. Option premiums are derived (same as backtest).
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
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from paper_trading.versions import VERSION_CONFIG
from paper_trading.dhan_broker import DhanBroker
from backtest.engine import BacktestEngine, BacktestResult
from backtest.real_data import derive_option_chain
from strategy.indicators import atr, rsi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

TRADES_DIR = Path("data/dhan_paper_trades")
TRADES_DIR.mkdir(parents=True, exist_ok=True)


class DhanPaperTrader:
    """Paper trader that uses the REAL BacktestEngine for signal + exit logic."""

    def __init__(self, version_id: str, detect_trend_fn, broker: DhanBroker,
                 capital: float = 50_000):
        self.version_id = version_id
        self.detect_trend = detect_trend_fn
        self.broker = broker
        self.capital = capital
        self.initial_capital = capital
        self.trades = []
        self.open_trade = None
        self.trades_today = 0
        self.last_trade_time = None
        self._last_buffer_refresh = None
        self._spot_buffer = pd.DataFrame()
        self._engine = None
        self._load_state()
        self._refresh_data()

    def _refresh_data(self):
        """Fetch real 5-min OHLC from Dhan and rebuild BacktestEngine."""
        try:
            today = datetime.now().date()
            from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            to_date = today.strftime("%Y-%m-%d")

            r = self.broker.dhan.intraday_minute_data(
                "13", "IDX_I", "INDEX", from_date, to_date, 5
            )

            if r.get("status") == "success" and r.get("data") and "open" in r["data"]:
                d = r["data"]
                IST_OFFSET = pd.Timedelta(hours=5, minutes=30)
                rows = []
                for i in range(len(d["open"])):
                    dt = pd.to_datetime(d["timestamp"][i], unit="s") + IST_OFFSET
                    rows.append({
                        "date": dt, "open": d["open"][i], "high": d["high"][i],
                        "low": d["low"][i], "close": d["close"][i],
                        "volume": d["volume"][i],
                    })

                df = pd.DataFrame(rows)
                df = df[(df["date"].dt.hour >= 9) & (df["date"].dt.hour < 16)]
                df = df[~((df["date"].dt.hour == 9) & (df["date"].dt.minute < 15))]
                df = df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
                self._spot_buffer = df

                # Derive option chain from real spot (same as backtest)
                options = derive_option_chain(df, "NIFTY")

                # Create BacktestEngine with this data — uses EXACT same logic
                # Override detect_trend with our version-specific one
                from strategy import indicators as ind_module
                original_detect = ind_module.detect_trend
                ind_module.detect_trend = self.detect_trend

                self._engine = BacktestEngine(
                    spot_data=df, options_data=options,
                    underlying="NIFTY", initial_capital=self.capital,
                )
                # Restore original
                ind_module.detect_trend = original_detect

                logger.info(f"[{self.version_id}] Loaded {len(df)} candles "
                            f"({df['date'].dt.date.nunique()} days). Engine ready.")
            else:
                logger.warning(f"[{self.version_id}] Data refresh failed: {r.get('data', {})}")
        except Exception as e:
            logger.error(f"[{self.version_id}] Refresh error: {e}")

    def tick(self, current_time: datetime = None, shared_spot: float = None):
        """Run one step of the backtest engine on accumulated data."""
        if current_time is None:
            current_time = datetime.now()

        if not shared_spot:
            return None

        # Refresh data from Dhan every 5 minutes
        needs_refresh = (
            self._last_buffer_refresh is None or
            (current_time - self._last_buffer_refresh).total_seconds() >= 300
        )
        if needs_refresh:
            self._refresh_data()
            self._last_buffer_refresh = current_time

        if self._engine is None or self._spot_buffer.empty:
            return None

        # Find the latest candle index in the engine's data
        spot = self._engine.spot
        if len(spot) < 50:
            return None

        # Use the last candle as current
        i = len(spot) - 1
        candle_time = spot.iloc[i]["date"]

        # Reset daily counter
        if self.last_trade_time and self.last_trade_time.date() != current_time.date():
            self.trades_today = 0
            self._engine._trades_today = 0

        # Sync engine state with our state
        self._engine.capital = self.capital
        self._engine._trades_today = self.trades_today

        # Run engine's monitoring on open trades
        if self._engine._open_trades:
            self._engine._monitor_positions(candle_time, shared_spot)
            # Check if engine closed any trades
            self._sync_closed_trades()

        # Run engine's scan if no open trade
        if not self._engine._open_trades and not self.open_trade:
            # Check daily limits
            if self.trades_today >= 2:
                self._log_gate("max_trades", f"Already {self.trades_today} trades today", current_time)
                return None

            # Cooldown check
            if self.last_trade_time:
                mins_since = (current_time - self.last_trade_time).total_seconds() / 60
                if mins_since < 30:
                    return None

            # Let the engine scan — uses exact same gates, indicators, SL logic
            old_trade_count = len(self._engine._closed_trades)
            old_open_count = len(self._engine._open_trades)

            self._engine._scan_and_signal(i, candle_time, shared_spot)

            # Check if engine opened a new trade
            if len(self._engine._open_trades) > old_open_count:
                trade = self._engine._open_trades[-1]
                self.open_trade = {
                    "id": len(self.trades) + 1,
                    "version": self.version_id,
                    "symbol": trade.tradingsymbol,
                    "type": "CE" if "CE" in trade.tradingsymbol else "PE",
                    "entry_price": round(trade.entry_price, 2),
                    "stop_loss": round(trade.stop_loss, 2),
                    "target": round(trade.target, 2),
                    "quantity": trade.quantity,
                    "entry_time": current_time.isoformat(),
                    "spot_at_entry": round(shared_spot, 2),
                    "trend_strength": trade.trend_strength,
                    "rsi": trade.context.get("trend", {}).get("rsi", 0),
                    "indicators": trade.context.get("trend", {}).get("details", {}),
                    "status": "OPEN",
                    "exit_price": None, "exit_time": None,
                    "exit_reason": None, "pnl": None,
                }
                self.trades_today += 1
                self.last_trade_time = current_time
                self._save_state()

                logger.info(f"[{self.version_id}] BUY {trade.tradingsymbol} "
                             f"@ {trade.entry_price:.2f} SL={trade.stop_loss:.2f} "
                             f"TGT={trade.target:.2f} Qty={trade.quantity} "
                             f"Trend={trade.trend_strength:.0f}%")
                return self.open_trade

        # Write live status
        self._write_live_status(shared_spot, current_time)
        return None

    def _sync_closed_trades(self):
        """Check if the backtest engine closed our open trade."""
        if not self.open_trade:
            return

        # Engine closed trades go to _closed_trades
        for trade in self._engine._closed_trades:
            # Match by tradingsymbol
            if (self.open_trade and
                trade.tradingsymbol == self.open_trade["symbol"] and
                trade.status == "CLOSED"):

                self.open_trade["exit_price"] = round(trade.exit_price, 2)
                self.open_trade["exit_time"] = datetime.now().isoformat()
                self.open_trade["exit_reason"] = trade.exit_reason
                self.open_trade["pnl"] = round(trade.net_pnl, 2)
                self.open_trade["status"] = "CLOSED"
                self.capital += trade.net_pnl

                logger.info(f"[{self.version_id}] SELL {trade.tradingsymbol} "
                             f"@ {trade.exit_price:.2f} {trade.exit_reason} "
                             f"P&L={trade.net_pnl:+,.0f} Capital={self.capital:,.0f}")

                self.trades.append(self.open_trade)
                self.open_trade = None
                self._engine._closed_trades.remove(trade)
                self._save_state()
                break

    def _log_gate(self, gate_name: str, reason: str, current_time: datetime):
        log_dir = Path(__file__).parent.parent / "data" / "gate_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{self.version_id}_{current_time.strftime('%Y-%m-%d')}.jsonl"
        entry = {"time": current_time.strftime("%H:%M"), "gate": gate_name, "reason": reason}
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _write_live_status(self, spot_price: float, current_time: datetime):
        try:
            status = {
                "version": self.version_id,
                "time": current_time.strftime("%H:%M:%S"),
                "spot": round(spot_price, 2),
                "candles": len(self._spot_buffer),
                "trades_today": self.trades_today,
                "capital": round(self.capital, 2),
                "pnl": round(self.capital - self.initial_capital, 2),
                "engine": "BacktestEngine" if self._engine else "None",
            }

            if self._engine and len(self._engine.spot) >= 50:
                trend = self._engine._get_trend(len(self._engine.spot) - 1)
                direction = "BULLISH" if trend["direction"] == 1 else (
                    "BEARISH" if trend["direction"] == -1 else "NEUTRAL")
                status["trend"] = direction
                status["trend_strength"] = trend.get("strength", 0)
                status["rsi"] = trend.get("rsi", 0)
                status["indicators"] = trend.get("details", {})

                # Decision reason
                details = trend.get("details", {})
                pa = details.get("price_action", "NEUTRAL")
                rsi_val = trend.get("rsi", 50)
                target_type = "CE" if trend["direction"] == 1 else "PE"

                if trend["direction"] == 0:
                    status["decision"] = "No clear trend — sitting out"
                elif trend["strength"] > 92 or (trend["direction"] == -1 and (100 - trend["strength"]) > 92):
                    status["decision"] = f"Trend exhausted ({trend['strength']:.0f}%)"
                elif target_type == "CE" and pa != "BULL":
                    status["decision"] = f"Price action {pa}, need BULL for CE"
                elif target_type == "PE" and pa != "BEAR":
                    status["decision"] = f"Price action {pa}, need BEAR for PE"
                elif self.trades_today >= 2:
                    status["decision"] = "Max trades for today"
                elif self.open_trade:
                    status["decision"] = f"In trade: {self.open_trade['symbol']}"
                else:
                    status["decision"] = f"Looking for {target_type} — gates passing"

            if self.open_trade:
                t = self.open_trade
                delta = 0.4 if t["type"] == "CE" else -0.4
                est_pnl = (spot_price - t["spot_at_entry"]) * delta * t["quantity"]
                status["open_trade"] = {
                    "symbol": t["symbol"], "type": t["type"],
                    "entry": t["entry_price"], "sl": t["stop_loss"],
                    "target": t["target"], "est_pnl": round(est_pnl, 0),
                }

            live_dir = Path(__file__).parent.parent / "data" / "live_status"
            live_dir.mkdir(parents=True, exist_ok=True)
            with open(live_dir / f"{self.version_id}.json", "w") as f:
                json.dump(status, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"[{self.version_id}] Live status error: {e}")

    def _save_state(self):
        state = {"version_id": self.version_id, "capital": self.capital,
                 "initial_capital": self.initial_capital, "trades": self.trades,
                 "open_trade": self.open_trade, "trades_today": self.trades_today,
                 "last_trade_time": self.last_trade_time.isoformat() if self.last_trade_time else None}
        with open(TRADES_DIR / f"{self.version_id}.json", "w") as f:
            json.dump(state, f, indent=2, default=str)

    def _load_state(self):
        path = TRADES_DIR / f"{self.version_id}.json"
        if path.exists():
            with open(path) as f:
                state = json.load(f)
            self.capital = state.get("capital", self.initial_capital)
            self.trades = state.get("trades", [])
            self.open_trade = state.get("open_trade")
            self.trades_today = state.get("trades_today", 0)
            lt = state.get("last_trade_time")
            self.last_trade_time = datetime.fromisoformat(lt) if lt else None

    def get_summary(self):
        closed = [t for t in self.trades if t["status"] == "CLOSED"]
        winners = [t for t in closed if (t.get("pnl") or 0) > 0]
        return {"version": self.version_id, "capital": round(self.capital, 2),
                "pnl": round(self.capital - self.initial_capital, 2),
                "roi": round((self.capital - self.initial_capital) / self.initial_capital * 100, 2),
                "trades": len(closed), "winners": len(winners),
                "losers": len(closed) - len(winners),
                "win_rate": round(len(winners) / max(len(closed), 1) * 100, 1),
                "open_trade": self.open_trade is not None}


def _write_daily_summary(traders: dict, date_str: str):
    summary_dir = Path(__file__).parent.parent / "data" / "daily_summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)
    gate_log_dir = Path(__file__).parent.parent / "data" / "gate_logs"
    summaries = {}
    for vid, trader in traders.items():
        s = trader.get_summary()
        today_trades = [t for t in trader.trades
                        if t.get("status") == "CLOSED" and
                        t.get("entry_time", "").startswith(date_str)]
        gate_counts = {}
        log_file = gate_log_dir / f"{vid}_{date_str}.jsonl"
        if log_file.exists():
            for line in log_file.read_text().strip().split("\n"):
                if not line: continue
                try:
                    entry = json.loads(line)
                    gate = entry.get("gate", "unknown")
                    gate_counts[gate] = gate_counts.get(gate, 0) + 1
                except Exception: pass
        winners = [t for t in today_trades if (t.get("pnl") or 0) > 0]
        losers = [t for t in today_trades if (t.get("pnl") or 0) < 0]
        summaries[vid] = {
            "version": vid, "date": date_str, "trades": len(today_trades),
            "winners": len(winners), "losers": len(losers),
            "pnl": round(sum(t.get("pnl", 0) for t in today_trades), 2),
            "capital": s["capital"], "total_pnl": s["pnl"],
            "gate_blocks": gate_counts, "trades_detail": today_trades,
        }
    with open(summary_dir / f"{date_str}.json", "w") as f:
        json.dump(summaries, f, indent=2, default=str)
    logger.info(f"Daily summary written for {date_str}")


def _write_token_alert(expired: bool):
    alert_file = Path(__file__).parent.parent / "data" / "token_alert.json"
    alert_file.parent.mkdir(parents=True, exist_ok=True)
    with open(alert_file, "w") as f:
        json.dump({
            "expired": expired,
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": "TOKEN EXPIRED — update at /update" if expired else "Token OK",
        }, f)


def main():
    parser = argparse.ArgumentParser(description="Dhan Paper Trading")
    parser.add_argument("--live", action="store_true", help="Start live paper trading")
    parser.add_argument("--status", action="store_true", help="Show current status")
    parser.add_argument("--dashboard", action="store_true", help="Generate dashboard")
    parser.add_argument("--reset", action="store_true", help="Reset all trades")
    parser.add_argument("--capital", type=float, default=50_000)
    args = parser.parse_args()

    if args.reset:
        for f in TRADES_DIR.glob("*.json"):
            f.unlink()
        print("Dhan paper trades reset.")
        return

    if args.status:
        for vid, cfg in VERSION_CONFIG.items():
            path = TRADES_DIR / f"{vid}.json"
            if path.exists():
                with open(path) as f:
                    state = json.load(f)
                pnl = state["capital"] - state["initial_capital"]
                closed = [t for t in state["trades"] if t["status"] == "CLOSED"]
                wins = sum(1 for t in closed if (t.get("pnl") or 0) > 0)
                wr = wins / max(len(closed), 1) * 100
                print(f"  {cfg['name']}: Capital={state['capital']:,.0f} P&L={pnl:+,.0f} "
                      f"Trades={len(closed)} WR={wr:.0f}%")
        return

    if args.live:
        broker = DhanBroker()
        traders = {}
        for vid, cfg in VERSION_CONFIG.items():
            detect_fn = cfg["detect_trend"]()
            traders[vid] = DhanPaperTrader(vid, detect_fn, broker, args.capital)

        logger.info(f"Starting Dhan paper trading — V1 + V3 (BacktestEngine), Rs.{args.capital:,.0f} each")
        logger.info("Polling: 2 min (scanning) / 1 min (when trade open)")
        logger.info("Press Ctrl+C to stop.\n")

        eod_summary_done = {}
        token_alert_written = False

        while True:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            if now.hour < 9 or (now.hour == 9 and now.minute < 15):
                time.sleep(60); continue

            # Daily summary at 3:35 PM
            if now.hour == 15 and now.minute >= 35 and today_str not in eod_summary_done:
                _write_daily_summary(traders, today_str)
                eod_summary_done[today_str] = True
                for vid, trader in traders.items():
                    s = trader.get_summary()
                    logger.info(f"[{vid}] EOD: P&L={s['pnl']:+,.0f} Trades={s['trades']} WR={s['win_rate']}%")

            if now.hour >= 16:
                time.sleep(3600); continue

            # Fetch spot ONCE
            try:
                spot_price = broker.get_spot_price("NIFTY")
                logger.info(f"NIFTY: {spot_price:.2f}")
                token_alert_written = False
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Spot fetch failed: {e}")
                if "808" in error_msg or "Authentication" in error_msg or "Invalid Token" in error_msg:
                    if not token_alert_written:
                        _write_token_alert(True)
                        token_alert_written = True
                        logger.error("TOKEN EXPIRED! Update at http://SERVER:8080/")
                time.sleep(60); continue

            if not token_alert_written:
                _write_token_alert(False)

            for vid, trader in traders.items():
                try:
                    trader.tick(now, shared_spot=spot_price)
                except Exception as e:
                    logger.error(f"[{vid}] Error: {e}")

            # Status
            any_open = False
            for vid, trader in traders.items():
                s = trader.get_summary()
                status = "OPEN" if s["open_trade"] else "idle"
                if s["open_trade"]: any_open = True
                logger.info(f"  [{vid[:6]}] P&L={s['pnl']:+,.0f} | {s['trades']} trades | {status}")

            if any_open:
                time.sleep(60)
            else:
                time.sleep(120)

    if args.dashboard:
        from paper_trading.run import generate_dashboard
        generate_dashboard()

    parser.print_help()


if __name__ == "__main__":
    main()
