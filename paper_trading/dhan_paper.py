#!/usr/bin/env python3
"""
Paper trading with Dhan — real market data, virtual orders.

Setup:
  1. Create free account at https://dhanhq.co
  2. Go to API section, generate access token
  3. Add to .env:
     DHAN_CLIENT_ID=your_client_id
     DHAN_ACCESS_TOKEN=your_access_token
  4. Enable "Virtual Trading" in Dhan app settings

Usage:
  # Start live paper trading (polls every 2 min):
  python -m paper_trading.dhan_paper --live

  # Check status:
  python -m paper_trading.dhan_paper --status

  # Generate dashboard:
  python -m paper_trading.dhan_paper --dashboard

  # Reset:
  python -m paper_trading.dhan_paper --reset
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from paper_trading.versions import VERSION_CONFIG
from paper_trading.dhan_broker import DhanBroker
from strategy.indicators import atr, rsi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

TRADES_DIR = Path("data/dhan_paper_trades")
TRADES_DIR.mkdir(parents=True, exist_ok=True)

CAPITAL_PER_VERSION = 50_000


class DhanPaperTrader:
    """Paper trader that uses Dhan API for real data + virtual orders."""

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
        self.spot_buffer = pd.DataFrame()
        self._load_state()
        self._preload_candles()

    def _preload_candles(self):
        """Load last 5 days of 5-min candles from Dhan on startup.
        This way the system is ready to trade immediately, no 4-hour wait."""
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

                self.spot_buffer = df
                logger.info(f"[{self.version_id}] Preloaded {len(df)} candles "
                            f"({df['date'].dt.date.nunique()} days). Ready to trade.")
            else:
                logger.warning(f"[{self.version_id}] Preload failed: {r.get('data', {})}")
        except Exception as e:
            logger.error(f"[{self.version_id}] Preload error: {e}")

    PARAMS = {
        "min_trend_strength": 60, "min_bear_strength": 45,
        "max_trend_strength": 92, "max_rsi_ce": 75,
        "rsi_dead_zone_lo": 48, "rsi_dead_zone_hi": 57,
        "max_trades_per_day": 2, "cooldown_minutes": 30,
        "skip_first_minutes": 5, "avoid_days": [0, 4], "avoid_hours": [9, 12],
        "min_sl_pct": 0.15, "atr_sl_multiplier": 1.5, "rr_ratio": 2.0,
        "breakeven_trigger_pct": 20, "max_loss_per_trade": 2000,
    }

    def tick(self, current_time: datetime = None, shared_spot: float = None):
        """Called every 2 minutes during market hours."""
        if current_time is None:
            current_time = datetime.now()

        # Use shared spot price (fetched once in main loop)
        if shared_spot:
            spot_price = shared_spot
        else:
            try:
                spot_price = self.broker.get_spot_price("NIFTY")
            except Exception as e:
                logger.error(f"[{self.version_id}] Spot fetch failed: {e}")
                return None

        # Add to buffer
        new_row = pd.DataFrame([{
            "date": current_time, "open": spot_price, "high": spot_price,
            "low": spot_price, "close": spot_price, "volume": 100000,
        }])
        self.spot_buffer = pd.concat([self.spot_buffer, new_row], ignore_index=True)

        # Keep last 500 candles
        if len(self.spot_buffer) > 500:
            self.spot_buffer = self.spot_buffer.iloc[-500:]

        # Write live status every tick
        self._write_live_status(spot_price, current_time)

        if self.open_trade:
            return self._monitor_position(spot_price, current_time)

        return self._scan_for_signal(spot_price, current_time)

    def _log_gate(self, gate_name: str, reason: str, current_time: datetime):
        """Log why a gate blocked a trade."""
        log_dir = Path(__file__).parent.parent / "data" / "gate_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{self.version_id}_{current_time.strftime('%Y-%m-%d')}.jsonl"
        entry = {"time": current_time.strftime("%H:%M"), "gate": gate_name, "reason": reason}
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _scan_for_signal(self, spot_price: float, current_time: datetime):
        """Run full gate check and generate signal. Logs every blocked gate."""
        hour = current_time.hour
        minute = current_time.minute
        time_min = hour * 60 + minute

        # Daily reset
        if self.last_trade_time and self.last_trade_time.date() != current_time.date():
            self.trades_today = 0

        # Market hours
        market_start = 9 * 60 + 15 + self.PARAMS["skip_first_minutes"]
        if time_min < market_start or hour > 15 or (hour == 15 and minute > 15):
            return None
        if current_time.weekday() in self.PARAMS["avoid_days"]:
            self._log_gate("avoid_day", f"Weekday {current_time.strftime('%A')} blocked", current_time)
            return None

        afternoon_relaxed = (time_min >= 13 * 60 + 30 and self.trades_today == 0)
        if not afternoon_relaxed and hour in self.PARAMS["avoid_hours"]:
            self._log_gate("avoid_hour", f"Hour {hour}:00 blocked", current_time)
            return None
        if self.trades_today >= self.PARAMS["max_trades_per_day"]:
            self._log_gate("max_trades", f"Already {self.trades_today} trades today", current_time)
            return None
        if self.last_trade_time:
            mins_since = (current_time - self.last_trade_time).total_seconds() / 60
            if mins_since < self.PARAMS["cooldown_minutes"]:
                self._log_gate("cooldown", f"{mins_since:.0f}m since last trade (<{self.PARAMS['cooldown_minutes']}m)", current_time)
                return None

        if len(self.spot_buffer) < 50:
            self._log_gate("candles", f"Only {len(self.spot_buffer)} candles (<50)", current_time)
            return None

        spot_5m = self.spot_buffer.set_index("date").resample("5min").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna().reset_index()

        if len(spot_5m) < 20:
            self._log_gate("5m_candles", f"Only {len(spot_5m)} 5-min candles (<20)", current_time)
            return None

        trend = self.detect_trend(spot_5m)

        if trend["direction"] == 0:
            self._log_gate("trend_neutral", f"No clear trend (strength={trend['strength']:.0f}%)", current_time)
            return None
        if trend["direction"] == 1 and trend["strength"] < self.PARAMS["min_trend_strength"]:
            self._log_gate("trend_weak_bull", f"Bull too weak ({trend['strength']:.0f}% < {self.PARAMS['min_trend_strength']}%)", current_time)
            return None
        if trend["direction"] == -1 and (100 - trend["strength"]) < self.PARAMS["min_bear_strength"]:
            self._log_gate("trend_weak_bear", f"Bear too weak ({100-trend['strength']:.0f}% < {self.PARAMS['min_bear_strength']}%)", current_time)
            return None
        if trend["direction"] == 1 and trend["strength"] > self.PARAMS["max_trend_strength"]:
            self._log_gate("trend_exhausted", f"Bull exhausted ({trend['strength']:.0f}% > {self.PARAMS['max_trend_strength']}%)", current_time)
            return None
        if trend["direction"] == -1 and (100 - trend["strength"]) > self.PARAMS["max_trend_strength"]:
            self._log_gate("trend_exhausted", f"Bear exhausted ({100-trend['strength']:.0f}% > {self.PARAMS['max_trend_strength']}%)", current_time)
            return None

        target_type = "CE" if trend["direction"] == 1 else "PE"
        rsi_val = trend.get("rsi", 50)
        if target_type == "CE" and rsi_val > self.PARAMS["max_rsi_ce"]:
            self._log_gate("rsi_overbought", f"RSI {rsi_val:.0f} > {self.PARAMS['max_rsi_ce']} for CE", current_time)
            return None
        if target_type == "PE" and rsi_val < 20:
            self._log_gate("rsi_oversold", f"RSI {rsi_val:.0f} < 20 for PE", current_time)
            return None
        if not afternoon_relaxed:
            if self.PARAMS["rsi_dead_zone_lo"] <= rsi_val <= self.PARAMS["rsi_dead_zone_hi"]:
                self._log_gate("rsi_dead_zone", f"RSI {rsi_val:.0f} in dead zone ({self.PARAMS['rsi_dead_zone_lo']}-{self.PARAMS['rsi_dead_zone_hi']})", current_time)
                return None

        pa = trend.get("details", {}).get("price_action", "NEUTRAL")
        if target_type == "CE" and pa != "BULL":
            self._log_gate("price_action", f"PA={pa}, need BULL for CE", current_time)
            return None
        if target_type == "PE" and pa != "BEAR":
            self._log_gate("price_action", f"PA={pa}, need BEAR for PE", current_time)
            return None

        self._log_gate("ALL_PASSED", f"Signal! {target_type} trend={trend['strength']:.0f}% RSI={rsi_val:.0f}", current_time)

        # ── Signal! Get real option data from Dhan ──
        try:
            chain = self.broker.get_option_chain("NIFTY")
        except Exception as e:
            logger.error(f"Option chain fetch failed: {e}")
            chain = []

        # Find best option near ATM
        strike_step = 50
        atm = round(spot_price / strike_step) * strike_step
        target_strike = atm + strike_step if target_type == "CE" else atm - strike_step

        # Try to get real LTP from chain
        premium = None
        security_id = ""
        if chain:
            for opt in chain:
                if (opt.get("strikePrice") == target_strike and
                    opt.get("optionType") == target_type):
                    premium = opt.get("LTP", opt.get("lastPrice", 0))
                    security_id = str(opt.get("securityId", ""))
                    break

        if not premium or premium < 50:
            # Fallback: estimate premium
            premium = max(50, abs(spot_price - target_strike) * 0.3 + 80)

        # SL / Target
        atr_val = atr(spot_5m, 14).iloc[-1] if len(spot_5m) >= 15 else 30
        if pd.isna(atr_val) or atr_val <= 0:
            atr_val = 30  # fallback to safe default
        sl_points = atr_val * self.PARAMS["atr_sl_multiplier"] * 0.4
        sl = round(premium - sl_points, 2)
        # Floor: SL never tighter than min_sl_pct of premium
        min_sl = round(premium * (1 - self.PARAMS["min_sl_pct"]), 2)
        sl = min(sl, min_sl)  # pick the LOWER (wider) SL
        # Cap: SL never wider than 50% loss
        sl = max(sl, premium * 0.50)
        risk = premium - sl
        if risk < premium * 0.05:  # minimum 5% risk (not zero)
            sl = round(premium * 0.85, 2)  # force 15% SL
            risk = premium - sl
        if risk <= 0: return None
        target = round(premium + risk * self.PARAMS["rr_ratio"], 2)
        qty = max(int(self.PARAMS["max_loss_per_trade"] / risk // 75) * 75, 75)

        symbol = f"NIFTY{int(target_strike)}{target_type}"

        # Place virtual order on Dhan (if security_id available)
        order_id = ""
        if security_id:
            try:
                result = self.broker.place_virtual_order(
                    security_id=security_id,
                    transaction_type="BUY",
                    quantity=qty,
                    order_type="MARKET",
                )
                order_id = result.get("data", {}).get("orderId", "")
            except Exception as e:
                logger.warning(f"Dhan virtual order failed (continuing as paper): {e}")

        trade = {
            "id": len(self.trades) + 1,
            "version": self.version_id,
            "symbol": symbol,
            "security_id": security_id,
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
            "order_id": order_id,
            "status": "OPEN",
            "exit_price": None, "exit_time": None, "exit_reason": None, "pnl": None,
        }

        self.open_trade = trade
        self.trades_today += 1
        self.last_trade_time = current_time
        self._save_state()

        logger.info(f"[{self.version_id}] BUY {symbol} @ {premium:.2f} "
                     f"SL={sl:.2f} TGT={target:.2f} Qty={qty} "
                     f"Trend={trend['strength']:.0f}% Dhan={order_id or 'paper'}")
        return trade

    def _monitor_position(self, spot_price: float, current_time: datetime):
        """Monitor open position using real spot data."""
        t = self.open_trade
        entry_spot = t["spot_at_entry"]
        delta = 0.4 if t["type"] == "CE" else -0.4
        est_premium = t["entry_price"] + (spot_price - entry_spot) * delta
        est_premium = max(est_premium, 0.5)

        # Try to get real option LTP
        if t.get("security_id"):
            try:
                quote = self.broker.get_option_ltp(t["security_id"])
                if quote and quote.get("LTP"):
                    est_premium = quote["LTP"]
            except Exception:
                pass  # fall back to estimate

        if est_premium <= t["stop_loss"]:
            self._close_trade(t["stop_loss"], current_time, "SL_HIT")
        elif est_premium >= t["target"]:
            self._close_trade(t["target"], current_time, "TARGET_HIT")
        elif current_time.hour == 15 and current_time.minute >= 15:
            self._close_trade(est_premium, current_time, "EOD_SQUAREOFF")
        else:
            # Trailing SL
            total_move = t["target"] - t["entry_price"]
            current_move = est_premium - t["entry_price"]
            if total_move > 0:
                move_pct = current_move / total_move * 100
                if move_pct >= self.PARAMS["breakeven_trigger_pct"]:
                    t["stop_loss"] = max(t["stop_loss"], t["entry_price"])

    def _close_trade(self, exit_price, exit_time, reason):
        t = self.open_trade
        t["exit_price"] = round(exit_price, 2)
        t["exit_time"] = exit_time.isoformat()
        t["exit_reason"] = reason
        t["pnl"] = round((exit_price - t["entry_price"]) * t["quantity"], 2)
        t["status"] = "CLOSED"
        self.capital += t["pnl"]

        # Place exit order on Dhan
        if t.get("security_id"):
            try:
                self.broker.place_virtual_order(
                    security_id=t["security_id"],
                    transaction_type="SELL",
                    quantity=t["quantity"],
                    order_type="MARKET",
                )
            except Exception:
                pass

        self.trades.append(t)
        self.open_trade = None
        self._save_state()
        logger.info(f"[{self.version_id}] SELL {t['symbol']} @ {exit_price:.2f} "
                     f"{reason} P&L={t['pnl']:+,.0f} Capital={self.capital:,.0f}")

    def _write_live_status(self, spot_price: float, current_time: datetime):
        """Write agent's current thinking to a JSON file for the web dashboard."""
        try:
            status = {
                "version": self.version_id,
                "time": current_time.strftime("%H:%M:%S"),
                "spot": round(spot_price, 2),
                "candles": len(self.spot_buffer),
                "trades_today": self.trades_today,
                "capital": round(self.capital, 2),
                "pnl": round(self.capital - self.initial_capital, 2),
            }

            # Run trend detection if enough candles
            if len(self.spot_buffer) >= 50:
                spot_5m = self.spot_buffer.set_index("date").resample("5min").agg({
                    "open": "first", "high": "max", "low": "min",
                    "close": "last", "volume": "sum"
                }).dropna().reset_index()

                if len(spot_5m) >= 20:
                    trend = self.detect_trend(spot_5m)
                    direction = "BULLISH" if trend["direction"] == 1 else (
                        "BEARISH" if trend["direction"] == -1 else "NEUTRAL")
                    status["trend"] = direction
                    status["trend_strength"] = trend.get("strength", 0)
                    status["rsi"] = trend.get("rsi", 0)
                    status["indicators"] = trend.get("details", {})

                    # What the agent decided
                    reasons = []
                    if trend["direction"] == 0:
                        reasons.append("No clear trend — sitting out")
                    elif trend["strength"] > self.PARAMS["max_trend_strength"]:
                        reasons.append(f"Trend exhausted ({trend['strength']:.0f}%) — too late to enter")
                    else:
                        rsi_val = trend.get("rsi", 50)
                        target_type = "CE" if trend["direction"] == 1 else "PE"

                        if target_type == "CE" and rsi_val > self.PARAMS["max_rsi_ce"]:
                            reasons.append(f"RSI too high ({rsi_val:.0f}) for CE — overbought")
                        elif target_type == "PE" and rsi_val < 20:
                            reasons.append(f"RSI too low ({rsi_val:.0f}) for PE — oversold")
                        elif self.PARAMS["rsi_dead_zone_lo"] <= rsi_val <= self.PARAMS["rsi_dead_zone_hi"]:
                            reasons.append(f"RSI in dead zone ({rsi_val:.0f}) — no conviction")
                        else:
                            pa = trend.get("details", {}).get("price_action", "NEUTRAL")
                            if target_type == "CE" and pa != "BULL":
                                reasons.append(f"Price action is {pa}, not BULL — waiting for confirmation")
                            elif target_type == "PE" and pa != "BEAR":
                                reasons.append(f"Price action is {pa}, not BEAR — waiting for confirmation")
                            elif self.trades_today >= self.PARAMS["max_trades_per_day"]:
                                reasons.append("Max trades for today reached")
                            elif self.open_trade:
                                reasons.append(f"Already in a trade: {self.open_trade.get('symbol', '')}")
                            else:
                                reasons.append(f"Looking for {target_type} entry — all gates passing")

                    status["decision"] = " | ".join(reasons) if reasons else "Scanning..."
                else:
                    status["decision"] = f"Building 5-min candles ({len(spot_5m)}/20 needed)"
            else:
                status["decision"] = f"Accumulating candles ({len(self.spot_buffer)}/50 needed)"

            # Open trade info
            if self.open_trade:
                t = self.open_trade
                delta = 0.4 if t["type"] == "CE" else -0.4
                est_pnl = (spot_price - t["spot_at_entry"]) * delta * t["quantity"]
                status["open_trade"] = {
                    "symbol": t["symbol"],
                    "type": t["type"],
                    "entry": t["entry_price"],
                    "sl": t["stop_loss"],
                    "target": t["target"],
                    "est_pnl": round(est_pnl, 0),
                }

            # Write to file
            live_dir = Path(__file__).parent.parent / "data" / "live_status"
            live_dir.mkdir(parents=True, exist_ok=True)
            with open(live_dir / f"{self.version_id}.json", "w") as f:
                json.dump(status, f, indent=2, default=str)

        except Exception as e:
            logger.debug(f"[{self.version_id}] Live status write error: {e}")

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
    """Write end-of-day summary with gate statistics."""
    summary_dir = Path(__file__).parent.parent / "data" / "daily_summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)
    gate_log_dir = Path(__file__).parent.parent / "data" / "gate_logs"

    summaries = {}
    for vid, trader in traders.items():
        s = trader.get_summary()
        today_trades = [t for t in trader.trades
                        if t.get("status") == "CLOSED" and
                        t.get("entry_time", "").startswith(date_str)]

        # Count gate blocks from today's log
        gate_counts = {}
        log_file = gate_log_dir / f"{vid}_{date_str}.jsonl"
        if log_file.exists():
            for line in log_file.read_text().strip().split("\n"):
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    gate = entry.get("gate", "unknown")
                    gate_counts[gate] = gate_counts.get(gate, 0) + 1
                except Exception:
                    pass

        winners = [t for t in today_trades if (t.get("pnl") or 0) > 0]
        losers = [t for t in today_trades if (t.get("pnl") or 0) < 0]

        summaries[vid] = {
            "version": vid,
            "date": date_str,
            "trades": len(today_trades),
            "winners": len(winners),
            "losers": len(losers),
            "pnl": round(sum(t.get("pnl", 0) for t in today_trades), 2),
            "capital": s["capital"],
            "total_pnl": s["pnl"],
            "gate_blocks": gate_counts,
            "trades_detail": today_trades,
        }

    with open(summary_dir / f"{date_str}.json", "w") as f:
        json.dump(summaries, f, indent=2, default=str)

    logger.info(f"Daily summary written for {date_str}")


def _write_token_alert(expired: bool):
    """Write token status for the web dashboard."""
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

        logger.info(f"Starting Dhan paper trading — V1 + V3, Rs.{args.capital:,.0f} each")
        logger.info("Polling: 2 min (scanning) / 1 min (when trade open)")
        logger.info("Press Ctrl+C to stop.\n")

        eod_summary_done = {}
        token_alert_written = False

        while True:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            if now.hour < 9 or (now.hour == 9 and now.minute < 15):
                time.sleep(60); continue

            # ── #3: Daily summary at 3:35 PM ──
            if now.hour == 15 and now.minute >= 35 and today_str not in eod_summary_done:
                _write_daily_summary(traders, today_str)
                eod_summary_done[today_str] = True
                for vid, trader in traders.items():
                    s = trader.get_summary()
                    logger.info(f"[{vid}] EOD: P&L={s['pnl']:+,.0f} Trades={s['trades']} WR={s['win_rate']}%")

            if now.hour >= 16:
                time.sleep(3600); continue

            # Fetch spot ONCE, share across all versions
            try:
                spot_price = broker.get_spot_price("NIFTY")
                logger.info(f"NIFTY: {spot_price:.2f}")
                token_alert_written = False  # reset on success
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Spot fetch failed: {e}")

                # ── #4: Token expiry detection ──
                if "808" in error_msg or "Authentication" in error_msg or "Invalid Token" in error_msg:
                    if not token_alert_written:
                        _write_token_alert(True)
                        token_alert_written = True
                        logger.error("TOKEN EXPIRED! Update at http://SERVER:8080/")

                time.sleep(60); continue

            # Clear token alert on success
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
                if s["open_trade"]:
                    any_open = True
                logger.info(f"  [{vid[:6]}] P&L={s['pnl']:+,.0f} | {s['trades']} trades | {status}")

            # Adaptive polling: 1 min when trade open, 2 min when scanning
            if any_open:
                time.sleep(60)   # 1 min — tighter exit monitoring
            else:
                time.sleep(120)  # 2 min — normal scanning

    if args.dashboard:
        from paper_trading.run import generate_dashboard
        generate_dashboard()

    parser.print_help()


if __name__ == "__main__":
    main()
