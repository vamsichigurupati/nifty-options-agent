"""Paper trading engine — runs strategies on live Yahoo Finance data with fake capital."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

from strategy.indicators import atr, rsi
from config.instruments import UNDERLYING_MAP

logger = logging.getLogger(__name__)

TRADES_DIR = Path("data/paper_trades")
TRADES_DIR.mkdir(parents=True, exist_ok=True)


class PaperTrader:
    """Runs one strategy version with paper capital on live data."""

    def __init__(self, version_id: str, detect_trend_fn, capital: float = 50_000,
                 underlying: str = "NIFTY"):
        self.version_id = version_id
        self.detect_trend = detect_trend_fn
        self.capital = capital
        self.initial_capital = capital
        self.underlying = underlying
        self.trades = []
        self.open_trade = None
        self.trades_today = 0
        self.last_trade_time = None
        self._load_state()

    # ── Gate parameters (same across all versions) ──
    PARAMS = {
        "min_trend_strength": 60,
        "min_bear_strength": 45,
        "max_trend_strength": 92,
        "max_rsi_ce": 75,
        "rsi_dead_zone_lo": 48,
        "rsi_dead_zone_hi": 57,
        "max_trades_per_day": 2,
        "cooldown_minutes": 30,
        "skip_first_minutes": 5,
        "avoid_days": [0, 4],  # Mon, Fri
        "avoid_hours": [9, 12],
        "min_sl_pct": 0.15,
        "atr_sl_multiplier": 1.5,
        "rr_ratio": 2.0,
        "breakeven_trigger_pct": 20,
        "max_loss_per_trade": 2000,
    }

    def scan(self, spot_df: pd.DataFrame, current_time: datetime):
        """Scan for trade signal using accumulated spot data."""
        if self.open_trade:
            self._monitor_position(spot_df, current_time)
            return None

        hour = current_time.hour
        minute = current_time.minute
        time_min = hour * 60 + minute

        # Reset daily counter
        if self.last_trade_time and self.last_trade_time.date() != current_time.date():
            self.trades_today = 0

        # Market hours
        market_start = 9 * 60 + 15 + self.PARAMS["skip_first_minutes"]
        if time_min < market_start or hour > 15 or (hour == 15 and minute > 15):
            return None

        # Day/hour filters
        if current_time.weekday() in self.PARAMS["avoid_days"]:
            return None

        # Afternoon relaxed mode
        afternoon_relaxed = (time_min >= 13 * 60 + 30 and self.trades_today == 0)
        if not afternoon_relaxed and hour in self.PARAMS["avoid_hours"]:
            return None

        if self.trades_today >= self.PARAMS["max_trades_per_day"]:
            return None

        # Cooldown
        if self.last_trade_time:
            mins_since = (current_time - self.last_trade_time).total_seconds() / 60
            if mins_since < self.PARAMS["cooldown_minutes"]:
                return None

        if len(spot_df) < 50:
            return None

        # ── Run trend detection (version-specific) ──
        # Resample to 5-min for indicators
        spot_5m = spot_df.set_index("date").resample("5min").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna().reset_index()

        if len(spot_5m) < 20:
            return None

        trend = self.detect_trend(spot_5m)

        if trend["direction"] == 0:
            return None
        if trend["direction"] == 1 and trend["strength"] < self.PARAMS["min_trend_strength"]:
            return None
        if trend["direction"] == -1 and (100 - trend["strength"]) < self.PARAMS["min_bear_strength"]:
            return None
        if trend["direction"] == 1 and trend["strength"] > self.PARAMS["max_trend_strength"]:
            return None
        if trend["direction"] == -1 and (100 - trend["strength"]) > self.PARAMS["max_trend_strength"]:
            return None

        target_type = "CE" if trend["direction"] == 1 else "PE"

        # RSI gates
        rsi_val = trend.get("rsi", 50)
        if target_type == "CE" and rsi_val > self.PARAMS["max_rsi_ce"]:
            return None
        if target_type == "PE" and rsi_val < 20:
            return None

        if not afternoon_relaxed:
            if self.PARAMS["rsi_dead_zone_lo"] <= rsi_val <= self.PARAMS["rsi_dead_zone_hi"]:
                return None

        # Price action strict gate
        pa = trend.get("details", {}).get("price_action", "NEUTRAL")
        if target_type == "CE" and pa != "BULL":
            return None
        if target_type == "PE" and pa != "BEAR":
            return None

        # ── Signal found ──
        spot_price = spot_df.iloc[-1]["close"]
        strike_step = UNDERLYING_MAP[self.underlying]["strike_step"]
        atm = round(spot_price / strike_step) * strike_step

        # Estimate option premium (ATM slightly OTM)
        if target_type == "CE":
            strike = atm + strike_step
        else:
            strike = atm - strike_step

        # Rough BS estimate for premium
        premium = max(50, abs(spot_price - strike) * 0.3 + 80)

        # ATR-based SL
        atr_val = atr(spot_5m, 14).iloc[-1] if len(spot_5m) >= 15 else 30
        sl_points = atr_val * self.PARAMS["atr_sl_multiplier"] * 0.4
        sl = round(max(premium - sl_points, premium * (1 - self.PARAMS["min_sl_pct"])), 2)
        sl = max(sl, premium * 0.50)

        risk = premium - sl
        if risk <= 0:
            return None
        target = round(premium + risk * self.PARAMS["rr_ratio"], 2)

        # Position sizing
        qty = max(int(self.PARAMS["max_loss_per_trade"] / risk // 75) * 75, 75)

        symbol = f"{self.underlying}{int(strike)}{target_type}"

        trade = {
            "id": len(self.trades) + 1,
            "version": self.version_id,
            "symbol": symbol,
            "type": target_type,
            "entry_price": round(premium, 2),
            "stop_loss": round(sl, 2),
            "target": round(target, 2),
            "quantity": qty,
            "entry_time": current_time.isoformat(),
            "spot_at_entry": round(spot_price, 2),
            "trend_strength": trend["strength"],
            "rsi": rsi_val,
            "indicators": trend.get("details", {}),
            "status": "OPEN",
            "exit_price": None,
            "exit_time": None,
            "exit_reason": None,
            "pnl": None,
        }

        self.open_trade = trade
        self.trades_today += 1
        self.last_trade_time = current_time
        self._save_state()

        logger.info(f"[{self.version_id}] SIGNAL: {target_type} {symbol} @ {premium:.2f} "
                     f"SL={sl:.2f} TGT={target:.2f} Trend={trend['strength']:.0f}%")
        return trade

    def _monitor_position(self, spot_df: pd.DataFrame, current_time: datetime):
        """Check SL/target/trailing on open position."""
        if not self.open_trade:
            return

        t = self.open_trade
        current_spot = spot_df.iloc[-1]["close"]

        # Estimate current option premium from spot movement
        entry_spot = t["spot_at_entry"]
        spot_move = current_spot - entry_spot
        delta = 0.4 if t["type"] == "CE" else -0.4
        estimated_premium = t["entry_price"] + spot_move * delta
        estimated_premium = max(estimated_premium, 0.5)

        # SL hit
        if estimated_premium <= t["stop_loss"]:
            self._close_trade(t["stop_loss"], current_time, "SL_HIT")
            return

        # Target hit
        if estimated_premium >= t["target"]:
            self._close_trade(t["target"], current_time, "TARGET_HIT")
            return

        # EOD squareoff
        if current_time.hour == 15 and current_time.minute >= 15:
            self._close_trade(estimated_premium, current_time, "EOD_SQUAREOFF")
            return

        # Trailing SL
        total_move = t["target"] - t["entry_price"]
        current_move = estimated_premium - t["entry_price"]
        if total_move > 0:
            move_pct = current_move / total_move * 100
            if move_pct >= self.PARAMS["breakeven_trigger_pct"]:
                t["stop_loss"] = max(t["stop_loss"], t["entry_price"])

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

        logger.info(f"[{self.version_id}] EXIT: {t['symbol']} @ {exit_price:.2f} "
                     f"Reason={reason} P&L={t['pnl']:+,.0f} Capital={self.capital:,.0f}")

    def _save_state(self):
        state = {
            "version_id": self.version_id,
            "capital": self.capital,
            "initial_capital": self.initial_capital,
            "trades": self.trades,
            "open_trade": self.open_trade,
            "trades_today": self.trades_today,
            "last_trade_time": self.last_trade_time.isoformat() if self.last_trade_time else None,
        }
        path = TRADES_DIR / f"{self.version_id}.json"
        with open(path, "w") as f:
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
            logger.info(f"[{self.version_id}] Loaded: {len(self.trades)} trades, "
                         f"capital={self.capital:,.0f}")

    def get_summary(self) -> dict:
        closed = [t for t in self.trades if t["status"] == "CLOSED"]
        winners = [t for t in closed if t["pnl"] > 0]
        losers = [t for t in closed if t["pnl"] <= 0]
        return {
            "version": self.version_id,
            "capital": round(self.capital, 2),
            "pnl": round(self.capital - self.initial_capital, 2),
            "roi": round((self.capital - self.initial_capital) / self.initial_capital * 100, 2),
            "trades": len(closed),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(len(winners) / max(len(closed), 1) * 100, 1),
            "open_trade": self.open_trade is not None,
        }
