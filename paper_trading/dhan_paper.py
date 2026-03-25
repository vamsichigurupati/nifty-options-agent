#!/usr/bin/env python3
"""
Paper trading with Dhan — BacktestEngine for SIGNALS, direct monitoring for EXITS.

Signal generation (gates, indicators, SL/target) uses BacktestEngine code.
Exit monitoring (SL check, target check, trailing, EOD) runs directly on spot price.
This avoids the engine-rebuild-loses-open-trade bug.
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
from backtest.engine import BacktestEngine
from backtest.real_data import derive_option_chain

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

TRADES_DIR = Path("data/dhan_paper_trades")
TRADES_DIR.mkdir(parents=True, exist_ok=True)
IST_OFFSET = pd.Timedelta(hours=5, minutes=30)


class DhanPaperTrader:
    """Signal from BacktestEngine, exits monitored directly."""

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
        self._last_refresh = None
        self._spot_buffer = pd.DataFrame()
        self._load_state()
        self._refresh_spot()

    def _refresh_spot(self):
        """Fetch real 5-min OHLC from Dhan."""
        try:
            today = datetime.now().date()
            from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            to_date = today.strftime("%Y-%m-%d")
            r = self.broker.dhan.intraday_minute_data(
                "13", "IDX_I", "INDEX", from_date, to_date, 5)
            if r.get("status") == "success" and r.get("data") and "open" in r["data"]:
                d = r["data"]
                rows = []
                for i in range(len(d["open"])):
                    rows.append({
                        "date": pd.to_datetime(d["timestamp"][i], unit="s") + IST_OFFSET,
                        "open": d["open"][i], "high": d["high"][i],
                        "low": d["low"][i], "close": d["close"][i],
                        "volume": d["volume"][i],
                    })
                df = pd.DataFrame(rows)
                df = df[(df["date"].dt.hour >= 9) & (df["date"].dt.hour < 16)]
                df = df[~((df["date"].dt.hour == 9) & (df["date"].dt.minute < 15))]
                df = df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
                self._spot_buffer = df
                logger.info(f"[{self.version_id}] Loaded {len(df)} candles ({df['date'].dt.date.nunique()} days)")
        except Exception as e:
            logger.error(f"[{self.version_id}] Refresh error: {e}")

    def _generate_signal(self, spot_price: float, current_time: datetime):
        """Use BacktestEngine to generate a signal (gates + SL + target)."""
        if len(self._spot_buffer) < 50:
            return None

        # Build a temporary engine just for signal generation
        options = derive_option_chain(self._spot_buffer, "NIFTY")
        engine = BacktestEngine(
            spot_data=self._spot_buffer, options_data=options,
            underlying="NIFTY", initial_capital=self.capital,
            detect_trend_fn=self.detect_trend,
        )

        i = len(engine.spot) - 1
        candle_time = engine.spot.iloc[i]["date"]

        # Run the scan
        engine._scan_and_signal(i, candle_time, spot_price)

        # Check if engine generated a trade
        if engine._open_trades:
            trade = engine._open_trades[0]
            return {
                "symbol": trade.tradingsymbol,
                "type": "CE" if "CE" in trade.tradingsymbol else "PE",
                "entry_price": round(trade.entry_price, 2),
                "stop_loss": round(trade.stop_loss, 2),
                "target": round(trade.target, 2),
                "quantity": trade.quantity,
                "trend_strength": trade.trend_strength,
                "rsi": trade.context.get("trend", {}).get("rsi", 0),
                "indicators": trade.context.get("trend", {}).get("details", {}),
            }
        return None

    def tick(self, current_time: datetime = None, shared_spot: float = None):
        if current_time is None:
            current_time = datetime.now()
        if not shared_spot:
            return None

        # Refresh spot data every 5 min
        if self._last_refresh is None or (current_time - self._last_refresh).total_seconds() >= 300:
            self._refresh_spot()
            self._last_refresh = current_time

        # Reset daily counter
        if self.last_trade_time and self.last_trade_time.date() != current_time.date():
            self.trades_today = 0

        # ── MONITOR OPEN TRADE ──
        if self.open_trade:
            self._monitor_exit(shared_spot, current_time)
            self._write_live_status(shared_spot, current_time)
            return None

        # ── SCAN FOR NEW SIGNAL ──
        # Market hours check
        hour, minute = current_time.hour, current_time.minute
        if hour > 15 or (hour == 15 and minute > 15):
            return None
        if hour < 9 or (hour == 9 and minute < 20):
            return None

        # Daily limit
        if self.trades_today >= 2:
            return None

        # Cooldown
        if self.last_trade_time:
            if (current_time - self.last_trade_time).total_seconds() / 60 < 30:
                return None

        # Generate signal using BacktestEngine
        signal = self._generate_signal(shared_spot, current_time)
        if signal:
            self.open_trade = {
                "id": len(self.trades) + 1,
                "version": self.version_id,
                "symbol": signal["symbol"],
                "type": signal["type"],
                "entry_price": signal["entry_price"],
                "stop_loss": signal["stop_loss"],
                "target": signal["target"],
                "quantity": signal["quantity"],
                "entry_time": current_time.isoformat(),
                "spot_at_entry": round(shared_spot, 2),
                "trend_strength": signal["trend_strength"],
                "rsi": signal["rsi"],
                "indicators": signal["indicators"],
                "trailing_sl": signal["stop_loss"],
                "status": "OPEN",
                "exit_price": None, "exit_time": None,
                "exit_reason": None, "pnl": None,
            }
            self.trades_today += 1
            self.last_trade_time = current_time
            self._save_state()
            logger.info(f"[{self.version_id}] BUY {signal['symbol']} @ {signal['entry_price']:.2f} "
                         f"SL={signal['stop_loss']:.2f} TGT={signal['target']:.2f} "
                         f"Qty={signal['quantity']} Trend={signal['trend_strength']:.0f}%")
            return self.open_trade

        self._write_live_status(shared_spot, current_time)
        return None

    def _monitor_exit(self, spot_price: float, current_time: datetime):
        """Direct exit monitoring — no engine needed. Runs on spot price."""
        t = self.open_trade
        if not t:
            return

        # Estimate current option premium from spot movement
        entry_spot = t["spot_at_entry"]
        delta = 0.4 if t["type"] == "CE" else -0.4
        est_premium = t["entry_price"] + (spot_price - entry_spot) * delta
        est_premium = max(est_premium, 0.5)

        entry = t["entry_price"]
        sl = t.get("trailing_sl", t["stop_loss"])
        target = t["target"]

        # 1. SL HIT
        if est_premium <= sl:
            self._close_trade(sl, current_time, "SL_HIT" if sl == t["stop_loss"] else "TRAILING_SL")
            return

        # 2. TARGET HIT
        if est_premium >= target:
            self._close_trade(target, current_time, "TARGET_HIT")
            return

        # 3. EOD SQUAREOFF (3:15 PM)
        if current_time.hour == 15 and current_time.minute >= 15:
            self._close_trade(est_premium, current_time, "EOD_SQUAREOFF")
            return

        # 4. TRAILING SL
        total_move = target - entry
        current_move = est_premium - entry
        if total_move > 0:
            move_pct = (current_move / total_move) * 100

            # Stage 1: Breakeven at 20%
            if move_pct >= 20:
                t["trailing_sl"] = max(t.get("trailing_sl", sl), entry)

            # Stage 2: Lock 30% at 50%
            if move_pct >= 50:
                new_sl = entry + current_move * 0.30
                t["trailing_sl"] = max(t.get("trailing_sl", sl), round(new_sl, 2))

            # Stage 3: Lock 50% at 70%
            if move_pct >= 70:
                new_sl = entry + current_move * 0.50
                t["trailing_sl"] = max(t.get("trailing_sl", sl), round(new_sl, 2))

            # Stage 4: Lock 70% at 85%
            if move_pct >= 85:
                new_sl = entry + current_move * 0.70
                t["trailing_sl"] = max(t.get("trailing_sl", sl), round(new_sl, 2))

        self._save_state()

    def _close_trade(self, exit_price: float, exit_time: datetime, reason: str):
        t = self.open_trade
        t["exit_price"] = round(exit_price, 2)
        t["exit_time"] = exit_time.isoformat()
        t["exit_reason"] = reason
        t["pnl"] = round((exit_price - t["entry_price"]) * t["quantity"], 2)
        t["status"] = "CLOSED"
        self.capital += t["pnl"]
        self.trades.append(t)
        self.open_trade = None
        self._save_state()
        logger.info(f"[{self.version_id}] SELL {t['symbol']} @ {exit_price:.2f} "
                     f"{reason} P&L={t['pnl']:+,.0f} Capital={self.capital:,.0f}")

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
            }
            if self.open_trade:
                t = self.open_trade
                delta = 0.4 if t["type"] == "CE" else -0.4
                est_pnl = (spot_price - t["spot_at_entry"]) * delta * t["quantity"]
                status["open_trade"] = {
                    "symbol": t["symbol"], "type": t["type"],
                    "entry": t["entry_price"], "sl": t.get("trailing_sl", t["stop_loss"]),
                    "target": t["target"], "est_pnl": round(est_pnl, 0),
                }
                status["decision"] = f"In trade: {t['symbol']} | Est P&L: {est_pnl:+,.0f}"
            else:
                status["decision"] = "Scanning..."

            live_dir = Path(__file__).parent.parent / "data" / "live_status"
            live_dir.mkdir(parents=True, exist_ok=True)
            with open(live_dir / f"{self.version_id}.json", "w") as f:
                json.dump(status, f, indent=2, default=str)
        except Exception:
            pass

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


def _write_daily_summary(traders, date_str):
    summary_dir = Path(__file__).parent.parent / "data" / "daily_summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summaries = {}
    for vid, trader in traders.items():
        s = trader.get_summary()
        today_trades = [t for t in trader.trades if t.get("status") == "CLOSED" and t.get("entry_time", "").startswith(date_str)]
        summaries[vid] = {"version": vid, "date": date_str, "trades": len(today_trades),
                          "pnl": round(sum(t.get("pnl", 0) for t in today_trades), 2),
                          "capital": s["capital"], "total_pnl": s["pnl"]}
    with open(summary_dir / f"{date_str}.json", "w") as f:
        json.dump(summaries, f, indent=2, default=str)
    logger.info(f"Daily summary written for {date_str}")


def _write_token_alert(expired):
    alert_file = Path(__file__).parent.parent / "data" / "token_alert.json"
    alert_file.parent.mkdir(parents=True, exist_ok=True)
    with open(alert_file, "w") as f:
        json.dump({"expired": expired, "time": datetime.now().strftime("%H:%M:%S")}, f)


def main():
    parser = argparse.ArgumentParser(description="Dhan Paper Trading")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--capital", type=float, default=50_000)
    args = parser.parse_args()

    if args.reset:
        for f in TRADES_DIR.glob("*.json"): f.unlink()
        print("Reset."); return

    if args.status:
        for vid, cfg in VERSION_CONFIG.items():
            path = TRADES_DIR / f"{vid}.json"
            if path.exists():
                state = json.load(open(path))
                pnl = state["capital"] - state["initial_capital"]
                closed = [t for t in state["trades"] if t["status"] == "CLOSED"]
                print(f"  {cfg['name']}: Capital={state['capital']:,.0f} P&L={pnl:+,.0f} Trades={len(closed)}")
        return

    if args.live:
        broker = DhanBroker()
        traders = {}
        for vid, cfg in VERSION_CONFIG.items():
            traders[vid] = DhanPaperTrader(vid, cfg["detect_trend"](), broker, args.capital)

        logger.info(f"Starting Dhan paper trading — V1 + V3, Rs.{args.capital:,.0f} each")
        logger.info("Signal: BacktestEngine | Exit: Direct monitoring")
        logger.info("Polling: 2 min scan / 1 min when trade open\n")

        eod_done = {}
        token_alert = False

        while True:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            if now.hour < 9 or (now.hour == 9 and now.minute < 15):
                time.sleep(60); continue

            if now.hour == 15 and now.minute >= 35 and today_str not in eod_done:
                _write_daily_summary(traders, today_str)
                eod_done[today_str] = True

            if now.hour >= 16:
                # Force close any open trades
                for vid, trader in traders.items():
                    if trader.open_trade:
                        try:
                            spot = broker.get_spot_price("NIFTY")
                            trader._monitor_exit(spot, now)
                        except Exception:
                            pass
                        if trader.open_trade:
                            trader._close_trade(trader.open_trade["entry_price"], now, "EOD_FORCE_CLOSE")
                time.sleep(3600); continue

            try:
                spot_price = broker.get_spot_price("NIFTY")
                logger.info(f"NIFTY: {spot_price:.2f}")
                token_alert = False
                _write_token_alert(False)
            except Exception as e:
                logger.error(f"Spot fetch failed: {e}")
                if "808" in str(e) or "Authentication" in str(e):
                    if not token_alert:
                        _write_token_alert(True)
                        token_alert = True
                time.sleep(60); continue

            any_open = False
            for vid, trader in traders.items():
                try:
                    trader.tick(now, shared_spot=spot_price)
                except Exception as e:
                    logger.error(f"[{vid}] Error: {e}")
                s = trader.get_summary()
                status = "OPEN" if s["open_trade"] else "idle"
                if s["open_trade"]: any_open = True
                logger.info(f"  [{vid[:6]}] P&L={s['pnl']:+,.0f} | {s['trades']} trades | {status}")

            time.sleep(60 if any_open else 120)

    parser.print_help()


if __name__ == "__main__":
    main()
