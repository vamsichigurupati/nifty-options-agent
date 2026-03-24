"""Core backtesting engine — simulates the trading loop on historical data."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from scipy.stats import norm
from config.settings import (
    MAX_LOSS_PER_TRADE, MAX_LOSS_PER_DAY, MAX_OPEN_POSITIONS,
    MIN_RISK_REWARD_RATIO, MIN_OPTION_PREMIUM, STRIKES_RANGE,
    TRAILING_SL_TRIGGER_PERCENT, TRAILING_SL_PROFIT_LOCK_PERCENT,
    MAX_HOLDING_MINUTES, BROKERAGE_PER_ORDER, STT_PERCENT_SELL,
    GST_PERCENT, STAMP_DUTY_PERCENT, SEBI_CHARGES_PERCENT,
    EXCHANGE_TXN_PERCENT
)
from config.instruments import UNDERLYING_MAP

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    trade_id: int
    tradingsymbol: str
    action: str
    entry_price: float
    entry_time: datetime
    quantity: int
    lot_size: int
    stop_loss: float
    target: float
    strategy: str
    underlying: str = ""
    exit_price: float = 0.0
    exit_time: datetime = None
    exit_reason: str = ""
    pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    max_favorable: float = 0.0  # max unrealized profit
    max_adverse: float = 0.0    # max unrealized loss
    trailing_sl: float = 0.0
    score: float = 0.0          # entry score for analysis
    trend_strength: float = 0.0 # trend strength at entry
    partial_exited: bool = False # partial profit booked?
    original_qty: int = 0       # qty before partial exit
    status: str = "OPEN"
    # v3: full context for trade drill-down
    context: dict = field(default_factory=dict)


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    daily_pnl: list[dict] = field(default_factory=list)
    params: dict = field(default_factory=dict)


def black_scholes_greeks(S, K, T, r, sigma, option_type="CE"):
    if T <= 0 or sigma <= 0:
        return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0}
    d1 = (np.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if option_type == "CE":
        delta = float(norm.cdf(d1))
    else:
        delta = float(norm.cdf(d1) - 1)
    gamma = float(norm.pdf(d1) / (S * sigma * np.sqrt(T)))
    return {"delta": delta, "gamma": gamma}


def calculate_charges(entry_price, exit_price, quantity):
    turnover = (entry_price + exit_price) * quantity
    brokerage = min(BROKERAGE_PER_ORDER * 2, turnover * 0.0003)
    stt = exit_price * quantity * STT_PERCENT_SELL / 100
    gst = brokerage * GST_PERCENT
    stamp = entry_price * quantity * STAMP_DUTY_PERCENT / 100
    sebi = turnover * SEBI_CHARGES_PERCENT / 100
    exchange = turnover * EXCHANGE_TXN_PERCENT / 100
    return round(brokerage + stt + gst + stamp + sebi + exchange, 2)


class BacktestEngine:
    """Simulates the full trading loop on historical data.

    Usage:
        engine = BacktestEngine(spot_df, options_dict)
        result = engine.run()
        # result.trades has all trades, result.equity_curve has the equity curve
    """

    def __init__(self, spot_data: pd.DataFrame, options_data: dict[str, pd.DataFrame],
                 underlying: str = "NIFTY", initial_capital: float = 100_000,
                 strategies: list[str] = None, params: dict = None,
                 detect_trend_fn=None):
        """
        Args:
            spot_data: DataFrame with date, open, high, low, close, volume
            options_data: dict mapping tradingsymbol -> DataFrame with same columns + oi, strike, instrument_type
            underlying: 'NIFTY' or 'BANKNIFTY'
            initial_capital: starting capital in INR
            strategies: list of strategy names to test. Default: all
            params: override default parameters (dict)
        """
        self.spot = spot_data.copy()
        self.spot["date"] = pd.to_datetime(self.spot["date"])
        self.spot = self.spot.sort_values("date").reset_index(drop=True)

        self.options = {}
        for sym, df in options_data.items():
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            self.options[sym] = df.sort_values("date").reset_index(drop=True)

        # Detect data resolution and build 5-min resampled view for indicators
        if len(self.spot) > 2:
            gap = (self.spot["date"].iloc[1] - self.spot["date"].iloc[0]).total_seconds() / 60
            self._candle_minutes = int(gap) if gap > 0 else 5
        else:
            self._candle_minutes = 5

        if self._candle_minutes < 5:
            # Resample to 5-min for indicator calculations
            spot_rs = self.spot.set_index("date").resample("5min").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum"
            }).dropna().reset_index()
            self.spot_5m = spot_rs
            # Build mapping: for each 1-min candle, which 5-min bar does it belong to?
            self._5m_index_map = {}
            for i, row in self.spot.iterrows():
                # Find the 5-min bar that contains this timestamp
                ts = row["date"]
                bar_start = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
                matches = self.spot_5m.index[self.spot_5m["date"] == bar_start]
                if len(matches) > 0:
                    self._5m_index_map[i] = matches[0]
        else:
            self.spot_5m = self.spot
            self._5m_index_map = None

        self.underlying = underlying
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.strategies = strategies or ["oi_reversal"]
        self._custom_detect_trend = detect_trend_fn  # version-specific, or None for default

        # Override params
        self.p = {
            "max_loss_per_trade": MAX_LOSS_PER_TRADE,
            "max_loss_per_day": MAX_LOSS_PER_DAY,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "min_rr": MIN_RISK_REWARD_RATIO,
            "min_premium": MIN_OPTION_PREMIUM,
            "strikes_range": STRIKES_RANGE,
            "sl_pct": 0.30,
            "rr_ratio": 2.0,
            "trailing_trigger_pct": TRAILING_SL_TRIGGER_PERCENT,
            "trailing_lock_pct": TRAILING_SL_PROFIT_LOCK_PERCENT,
            "max_holding_minutes": MAX_HOLDING_MINUTES,
            "scan_interval_candles": 2,   # every 2 candles (2 min with 1-min data)
            "min_volume": 10_000,
            "min_oi": 50_000,
            "min_score": 50,
            "min_trend_strength": 60,     # require 60%+ indicator agreement
            "cooldown_candles": 30,       # 30-min cooldown (30 x 1-min candles)
            "atr_sl_multiplier": 1.5,     # SL based on ATR, not fixed %
            # ── v2 improvements ──
            "partial_exit_pct": 50,       # book 50% qty at 50% of target
            "breakeven_trigger_pct": 20,  # trail to breakeven at 20% (real data has smaller moves)
            "avoid_hours": [9, 12],       # skip 9 AM (opening whipsaw) and 12 PM (lunch false breakouts)
            "avoid_days": [0, 4],         # skip Monday (weekend gap) and Friday
            "min_hold_candles": 10,       # must hold at least 10 candles before target exit
            # ── v5: real data fixes ──
            "min_sl_pct": 0.15,           # wider SL floor (was 0.10) — survive initial noise
            "rsi_dead_zone_lo": 48,       # block RSI 48-57 entries (no conviction zone)
            "rsi_dead_zone_hi": 57,
            "use_daily_bias": True,       # only trade in direction of day's opening move
        }
        if params:
            self.p.update(params)

        self._trade_counter = 0
        self._trades_today = 0
        self._open_trades: list[BacktestTrade] = []
        self._closed_trades: list[BacktestTrade] = []
        self._equity_curve: list[dict] = []
        self._daily_pnl_tracker: dict[str, float] = {}
        self._last_trade_candle = -999  # cooldown tracker

    def run(self) -> BacktestResult:
        """Run the backtest across all candles."""
        logger.info(f"Starting backtest: {len(self.spot)} candles, "
                     f"{len(self.options)} options, strategies={self.strategies}")

        dates = self.spot["date"].tolist()
        prev_day = None

        for i, candle_time in enumerate(dates):
            current_day = candle_time.date()
            spot_row = self.spot.iloc[i]
            spot_price = spot_row["close"]

            # Reset daily P&L tracker and trade counter on new day
            if current_day != prev_day:
                if prev_day:
                    self._record_daily_pnl(prev_day)
                self._trades_today = 0
                prev_day = current_day

            # 1. Monitor open positions (check exits)
            self._monitor_positions(candle_time, spot_price)

            # 2. Check if we should scan for new trades
            hour = candle_time.hour
            minute = candle_time.minute

            # Market hours: skip first N minutes (configurable), end by 3:15 PM
            skip_first = self.p.get("skip_first_minutes", 5)  # default: skip first 5 min
            market_start_min = 9 * 60 + 15 + skip_first
            time_min = hour * 60 + minute
            if time_min < market_start_min:
                continue
            if hour > 15 or (hour == 15 and minute > 15):
                continue

            # Afternoon relaxed mode: after 1:30 PM with 0 trades today,
            # liberate some rules to allow at least 1 trade
            afternoon_relaxed = (
                self.p.get("afternoon_relaxed", True)
                and time_min >= 13 * 60 + 30
                and self._trades_today == 0
            )

            # Skip bad hours (unless afternoon relaxed)
            if not afternoon_relaxed and hour in self.p.get("avoid_hours", []):
                continue
            # Skip bad days (e.g., Friday) — never relaxed
            if candle_time.weekday() in self.p.get("avoid_days", []):
                continue

            # Max trades per day
            max_daily = self.p.get("max_trades_per_day", 2)
            if self._trades_today >= max_daily:
                continue

            # Don't scan if at max positions
            if len(self._open_trades) >= self.p["max_open_positions"]:
                continue

            # Don't scan if daily loss limit hit
            day_pnl = self._get_day_pnl(current_day)
            if day_pnl <= -self.p["max_loss_per_day"]:
                continue

            # 3. Scan option chain at this candle (with cooldown)
            candles_since_trade = i - self._last_trade_candle
            cooldown_ok = candles_since_trade >= self.p.get("cooldown_candles", 12)
            if i % self.p["scan_interval_candles"] == 0 and cooldown_ok:
                self._scan_and_signal(i, candle_time, spot_price, relaxed=afternoon_relaxed)

            # 4. Record equity
            unrealized = sum(self._unrealized_pnl(t, spot_price) for t in self._open_trades)
            self._equity_curve.append({
                "date": candle_time,
                "equity": self.capital + unrealized,
                "capital": self.capital,
                "open_positions": len(self._open_trades),
            })

        # Force close any remaining open trades
        if self._open_trades:
            last_time = dates[-1]
            for trade in list(self._open_trades):
                self._close_trade(trade, trade.entry_price * 0.95, last_time, "BACKTEST_END")

        if prev_day:
            self._record_daily_pnl(prev_day)

        result = BacktestResult(
            trades=self._closed_trades,
            equity_curve=self._equity_curve,
            daily_pnl=[{"date": k, "pnl": v} for k, v in sorted(self._daily_pnl_tracker.items())],
            params=self.p,
        )
        logger.info(f"Backtest complete: {len(self._closed_trades)} trades")
        return result

    # ── Scanning & Signal Generation ─────────────────────────

    def _get_trend(self, candle_index: int) -> dict:
        """Detect trend using 5-min resampled data for stable signals."""
        if self._custom_detect_trend:
            detect_trend = self._custom_detect_trend
        else:
            from strategy.indicators import detect_trend
        # Use 5-min bars for indicator calculation (avoids 1-min noise)
        if self._5m_index_map is not None and candle_index in self._5m_index_map:
            idx_5m = self._5m_index_map[candle_index]
            lookback = min(idx_5m + 1, 50)
            spot_slice = self.spot_5m.iloc[idx_5m - lookback + 1:idx_5m + 1].copy()
        else:
            lookback = min(candle_index + 1, 50)
            spot_slice = self.spot.iloc[candle_index - lookback + 1:candle_index + 1].copy()
        spot_slice = spot_slice.reset_index(drop=True)
        if len(spot_slice) < 20:
            return {"direction": 0, "strength": 0, "details": {}}
        return detect_trend(spot_slice)

    def _build_trade_context(self, candle_index: int, trend: dict,
                              atr_val: float, signal: dict) -> dict:
        """Capture full indicator snapshot + surrounding price data for drill-down."""
        from strategy.indicators import ema, rsi, macd, supertrend, atr as atr_fn

        # Grab surrounding spot candles (60 before, 60 after)
        start = max(0, candle_index - 60)
        end = min(len(self.spot), candle_index + 61)
        spot_window = self.spot.iloc[start:end].copy().reset_index(drop=True)
        entry_offset = candle_index - start  # index of entry candle in window

        close = spot_window["close"]
        dates_list = [d.isoformat() if hasattr(d, 'isoformat') else str(d) for d in spot_window["date"]]
        ohlc = {
            "dates": dates_list,
            "open": spot_window["open"].round(2).tolist(),
            "high": spot_window["high"].round(2).tolist(),
            "low": spot_window["low"].round(2).tolist(),
            "close": close.round(2).tolist(),
            "volume": spot_window["volume"].tolist(),
        }

        # Compute indicators on this window
        ema9 = ema(close, 9).round(2).tolist()
        ema21 = ema(close, 21).round(2).tolist()
        rsi_vals = rsi(close, 14).round(2).tolist()
        macd_data = macd(close)
        macd_line = macd_data["macd"].round(2).tolist()
        macd_signal = macd_data["signal"].round(2).tolist()
        macd_hist = macd_data["histogram"].round(2).tolist()
        st_data = supertrend(spot_window)
        st_line = [round(float(v), 2) for v in st_data["supertrend"].tolist()]
        st_dir = [int(v) for v in st_data["direction"].tolist()]
        atr_series = atr_fn(spot_window, 14).round(2).tolist()

        # Option price data around the trade
        opt_sym = signal.get("tradingsymbol", "")
        opt_df = self.options.get(opt_sym)
        opt_prices = {"dates": [], "close": [], "high": [], "low": []}
        if opt_df is not None:
            candle_time = self.spot.iloc[candle_index]["date"]
            # Get option candles around entry time
            opt_start_time = self.spot.iloc[start]["date"]
            opt_end_time = self.spot.iloc[end - 1]["date"]
            opt_window = opt_df[(opt_df["date"] >= opt_start_time) & (opt_df["date"] <= opt_end_time)]
            if not opt_window.empty:
                opt_prices["dates"] = [d.isoformat() if hasattr(d, 'isoformat') else str(d) for d in opt_window["date"]]
                opt_prices["close"] = opt_window["close"].round(2).tolist()
                opt_prices["high"] = opt_window["high"].round(2).tolist()
                opt_prices["low"] = opt_window["low"].round(2).tolist()

        # Score breakdown
        score_breakdown = {
            "oi": min(25, signal.get("oi", 0) / 40000),  # rough scale
            "volume": min(20, signal.get("volume", 0) / 5000),
            "premium": 10 if 100 <= signal.get("ltp", 0) <= 500 else 5,
            "trend_alignment": min(25, trend.get("strength", 0) / 4),
            "moneyness": 15 if signal.get("strike") else 0,
        }

        return {
            "entry_candle_index": entry_offset,
            "spot_ohlc": ohlc,
            "ema9": ema9,
            "ema21": ema21,
            "rsi": rsi_vals,
            "macd_line": macd_line,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "supertrend": st_line,
            "supertrend_dir": st_dir,
            "atr": atr_series,
            "atr_value": round(atr_val, 2),
            "opt_prices": opt_prices,
            "trend": trend,
            "score_breakdown": score_breakdown,
            "signal": {
                "tradingsymbol": signal.get("tradingsymbol", ""),
                "strike": signal.get("strike", 0),
                "instrument_type": signal.get("instrument_type", ""),
                "ltp": signal.get("ltp", 0),
                "oi": signal.get("oi", 0),
                "volume": signal.get("volume", 0),
                "score": signal.get("score", 0),
            },
        }

    def _get_atr_value(self, candle_index: int, period: int = 14) -> float:
        """Get ATR using 5-min bars for consistent SL sizing."""
        from strategy.indicators import atr
        # Always compute ATR on 5-min bars for consistent SL
        if self._5m_index_map is not None and candle_index in self._5m_index_map:
            idx_5m = self._5m_index_map[candle_index]
            lookback = min(idx_5m + 1, period + 5)
            spot_slice = self.spot_5m.iloc[idx_5m - lookback + 1:idx_5m + 1].copy()
        else:
            lookback = min(candle_index + 1, period + 5)
            spot_slice = self.spot.iloc[candle_index - lookback + 1:candle_index + 1].copy()
        atr_series = atr(spot_slice, period)
        val = atr_series.iloc[-1]
        if np.isnan(val) or val <= 0:
            return 30.0  # safe fallback — ~30 pts is typical NIFTY 5-min ATR
        return float(val)

    def _scan_and_signal(self, candle_index: int, candle_time: datetime,
                          spot_price: float, relaxed: bool = False):
        """Scan options with trend filter. If relaxed=True (afternoon, 0 trades today),
        loosen RSI dead zone and daily bias to find at least 1 trade."""

        # ── GATE 1: Trend filter ──
        trend = self._get_trend(candle_index)
        if trend["direction"] == 0:
            return
        # Bull needs 60%+ bull agreement, bear needs 45%+ bear agreement
        if trend["direction"] == 1 and trend["strength"] < self.p.get("min_trend_strength", 60):
            return
        if trend["direction"] == -1 and (100 - trend["strength"]) < self.p.get("min_bear_strength", 45):
            return

        # ── GATE 1b: Trend exhaustion filter ──
        # If ALL indicators agree (>90%), the trend is likely mature/exhausting — skip
        max_trend = self.p.get("max_trend_strength", 92)
        if trend["direction"] == 1 and trend["strength"] > max_trend:
            return
        if trend["direction"] == -1 and (100 - trend["strength"]) > max_trend:
            return

        # Determine which option type to buy based on trend
        if trend["direction"] == 1:
            target_type = "CE"  # bullish → buy calls
        else:
            target_type = "PE"  # bearish → buy puts

        # ── GATE 2: RSI exhaustion guard ──
        rsi_val = trend.get("rsi", 50)
        max_rsi_ce = self.p.get("max_rsi_ce", 75)
        if target_type == "CE" and rsi_val > max_rsi_ce:
            return  # Don't buy CE when overbought
        if target_type == "PE" and rsi_val < 20:
            return  # Don't buy PE when extremely oversold

        # ── GATE 2b: RSI dead zone (no conviction) — SKIPPED in relaxed mode ──
        if not relaxed:
            dz_lo = self.p.get("rsi_dead_zone_lo", 48)
            dz_hi = self.p.get("rsi_dead_zone_hi", 57)
            if dz_lo <= rsi_val <= dz_hi:
                return  # RSI 48-57 = indecisive, 20% WR on real data

        # ── GATE 2c: Daily bias filter — SKIPPED in relaxed mode ──
        if not relaxed and self.p.get("use_daily_bias", False):
            candle_time_val = self.spot.iloc[candle_index]["date"]
            today = candle_time_val.date()
            day_candles = self.spot[self.spot["date"].dt.date == today]
            if len(day_candles) >= 6:
                open_price = day_candles.iloc[0]["open"]
                price_30m = day_candles.iloc[min(5, len(day_candles)-1)]["close"]
                day_bullish = price_30m > open_price
                if target_type == "CE" and not day_bullish:
                    return
                if target_type == "PE" and day_bullish:
                    return

        # ── GATE 3: Price Action indicator MUST agree (strict) ──
        # The price_action indicator from detect_trend() checks if last 3 candles
        # are moving in direction. Every losing trade had this as NEUTRAL or opposing.
        price_action = trend.get("details", {}).get("price_action", "NEUTRAL")
        if target_type == "CE" and price_action != "BULL":
            return  # Price not actively rising — skip CE
        if target_type == "PE" and price_action != "BEAR":
            return  # Price not actively falling — skip PE

        # ── GATE 4: ATR-based stop loss ──
        atr_val = self._get_atr_value(candle_index)

        strike_step = UNDERLYING_MAP[self.underlying]["strike_step"]
        atm = round(spot_price / strike_step) * strike_step
        min_strike = atm - self.p["strikes_range"] * strike_step
        max_strike = atm + self.p["strikes_range"] * strike_step

        # Collect option data at this timestamp
        scored = []
        for sym, df in self.options.items():
            mask = df["date"] == candle_time
            if not mask.any():
                continue
            row = df[mask].iloc[0]

            strike = row.get("strike", 0)
            if not (min_strike <= strike <= max_strike):
                continue

            opt_type = row.get("instrument_type", "CE")
            # Only consider options aligned with trend
            if opt_type != target_type:
                continue

            ltp = row["close"]
            if ltp < self.p["min_premium"]:
                continue
            # Max premium cap (avoid deep ITM with tiny SL%)
            max_prem = self.p.get("max_premium", 9999)
            if ltp > max_prem:
                continue

            volume = row.get("volume", 0)
            oi = row.get("oi", 0)
            lot_size = row.get("lot_size", 75)

            score = self._score_option(ltp, volume, oi, strike, spot_price, opt_type, trend)
            if score < self.p["min_score"]:
                continue

            scored.append({
                "tradingsymbol": sym,
                "strike": strike,
                "instrument_type": opt_type,
                "ltp": ltp,
                "volume": volume,
                "oi": oi,
                "lot_size": lot_size,
                "score": score,
            })

        if not scored:
            return

        scored.sort(key=lambda x: x["score"], reverse=True)

        # Try strategies (now trend-aware)
        for strategy in self.strategies:
            signal = None
            if strategy == "momentum":
                signal = self._momentum_strategy(scored, spot_price, atr_val, trend)
            elif strategy == "oi_reversal":
                signal = self._oi_reversal_strategy(scored, spot_price, atr_val, trend)

            if signal:
                signal["trend_strength"] = trend.get("strength", 0)
                context = self._build_trade_context(candle_index, trend, atr_val, signal)
                self._execute_signal(signal, candle_time, strategy, context)
                self._last_trade_candle = candle_index
                self._trades_today += 1
                return

    def _score_option(self, ltp, volume, oi, strike, spot, opt_type, trend) -> float:
        score = 0.0
        # OI
        if oi > 1_000_000: score += 20
        elif oi > 500_000: score += 15
        elif oi > 100_000: score += 10
        elif oi > 50_000: score += 5
        # Volume
        if volume > 100_000: score += 15
        elif volume > 50_000: score += 10
        elif volume > 10_000: score += 5
        # Premium range
        if 100 <= ltp <= 500: score += 10
        elif 50 <= ltp <= 1000: score += 5
        # Moneyness (slightly OTM preferred)
        moneyness = abs(spot - strike) / spot
        if 0.005 <= moneyness <= 0.02: score += 15
        elif moneyness <= 0.04: score += 10

        # NEW: Trend alignment bonus (up to 25 pts)
        strength = trend.get("strength", 0)
        if strength >= 83: score += 25       # 5/6 indicators agree
        elif strength >= 66: score += 15     # 4/6
        elif strength >= 50: score += 5

        # NEW: Volume/OI ratio (smart money activity)
        if oi > 0:
            vol_oi = volume / oi
            if 0.1 <= vol_oi <= 0.5: score += 10
            elif vol_oi <= 1.0: score += 5

        return score

    def _momentum_strategy(self, scored: list[dict], spot_price: float,
                            atr_val: float, trend: dict) -> dict | None:
        """Momentum: buy WITH the trend, ATR-based SL."""
        for opt in scored:
            if opt["volume"] < self.p["min_volume"]:
                continue

            # ATR-based SL: translate underlying ATR to option premium SL
            # Option moves ~delta * underlying_move, so SL in option terms:
            # We use a fraction of premium as SL, but capped by ATR logic
            atr_sl_mult = self.p.get("atr_sl_multiplier", 1.5)
            min_sl_pct = self.p.get("min_sl_pct", 0.10)  # floor: SL at least X% of premium

            est_delta = 0.4
            sl_points = atr_val * atr_sl_mult * est_delta
            sl = round(opt["ltp"] - sl_points, 2)

            # Enforce minimum SL distance (floor at min_sl_pct)
            min_sl = round(opt["ltp"] * (1 - min_sl_pct), 2)
            sl = min(sl, min_sl)
            sl = max(sl, opt["ltp"] * 0.50)  # cap at 50% loss

            risk = opt["ltp"] - sl
            # Force minimum 5% risk — never enter with near-zero risk
            if risk < opt["ltp"] * 0.05:
                sl = round(opt["ltp"] * 0.85, 2)
                risk = opt["ltp"] - sl
            if risk <= 0:
                continue
            target = round(opt["ltp"] + risk * self.p["rr_ratio"], 2)
            rr = (target - opt["ltp"]) / risk

            if rr < self.p["min_rr"]:
                continue

            qty = self._position_size(opt["ltp"], sl, opt["lot_size"])
            potential_loss = risk * qty
            if potential_loss > self.p["max_loss_per_trade"]:
                continue

            return {**opt, "action": "BUY", "stop_loss": sl,
                    "target": target, "quantity": qty}
        return None

    def _oi_reversal_strategy(self, scored: list[dict], spot_price: float,
                               atr_val: float, trend: dict) -> dict | None:
        """OI reversal: only when OI buildup confirms trend direction."""
        # For bullish trend: need high PE OI (support) → buy CE
        # For bearish trend: need high CE OI (resistance) → buy PE
        if trend["direction"] == 1:
            # Look for PE support in all options (not just scored, which is filtered to CE)
            # Use scored CEs directly since they're already trend-filtered
            pass
        else:
            pass

        # Since scored is already filtered to trend-aligned type, just pick best
        for opt in scored:
            if opt["oi"] < 200_000:
                continue
            atr_sl_mult = self.p.get("atr_sl_multiplier", 1.5)
            min_sl_pct = self.p.get("min_sl_pct", 0.10)
            est_delta = 0.4
            sl_points = atr_val * atr_sl_mult * est_delta
            sl = round(opt["ltp"] - sl_points, 2)
            min_sl = round(opt["ltp"] * (1 - min_sl_pct), 2)
            sl = min(sl, min_sl)
            sl = max(sl, opt["ltp"] * 0.50)

            risk = opt["ltp"] - sl
            # Force minimum 5% risk
            if risk < opt["ltp"] * 0.05:
                sl = round(opt["ltp"] * 0.85, 2)
                risk = opt["ltp"] - sl
            if risk <= 0:
                continue
            target = round(opt["ltp"] + risk * self.p["rr_ratio"], 2)
            rr = (target - opt["ltp"]) / risk
            if rr < self.p["min_rr"]:
                continue
            qty = self._position_size(opt["ltp"], sl, opt["lot_size"])
            return {**opt, "action": "BUY", "stop_loss": sl,
                    "target": target, "quantity": qty}
        return None

    def _position_size(self, entry, sl, lot_size) -> int:
        risk_per_unit = abs(entry - sl)
        if risk_per_unit <= 0:
            return lot_size
        max_units = self.p["max_loss_per_trade"] / risk_per_unit
        lots = max(int(max_units // lot_size), 1)
        return lots * lot_size

    def _execute_signal(self, signal: dict, candle_time: datetime,
                         strategy: str, context: dict = None):
        self._trade_counter += 1
        trade = BacktestTrade(
            trade_id=self._trade_counter,
            tradingsymbol=signal["tradingsymbol"],
            action=signal["action"],
            entry_price=signal["ltp"],
            entry_time=candle_time,
            quantity=signal["quantity"],
            lot_size=signal.get("lot_size", 75),
            stop_loss=signal["stop_loss"],
            target=signal["target"],
            strategy=strategy,
            underlying=self.underlying,
            trailing_sl=signal["stop_loss"],
            score=signal.get("score", 0),
            trend_strength=signal.get("trend_strength", 0),
            original_qty=signal["quantity"],
            context=context or {},
        )
        self._open_trades.append(trade)
        logger.debug(f"Trade #{trade.trade_id}: BUY {trade.tradingsymbol} "
                      f"@ {trade.entry_price:.2f} SL={trade.stop_loss:.2f} "
                      f"TGT={trade.target:.2f} Score={trade.score:.0f}")

    # ── Position Monitoring (v2: partial exits, smarter trailing) ──

    def _monitor_positions(self, candle_time: datetime, spot_price: float):
        for trade in list(self._open_trades):
            opt_df = self.options.get(trade.tradingsymbol)
            if opt_df is None:
                continue
            mask = opt_df["date"] == candle_time
            if not mask.any():
                continue
            row = opt_df[mask].iloc[0]
            high = row["high"]
            low = row["low"]
            close = row["close"]

            # Track MFE/MAE
            if trade.action == "BUY":
                unrealized_best = (high - trade.entry_price) * trade.original_qty
                unrealized_worst = (low - trade.entry_price) * trade.original_qty
            else:
                unrealized_best = (trade.entry_price - low) * trade.original_qty
                unrealized_worst = (trade.entry_price - high) * trade.original_qty
            trade.max_favorable = max(trade.max_favorable, unrealized_best)
            trade.max_adverse = min(trade.max_adverse, unrealized_worst)

            # Count candles held
            candles_held = 0
            entry_idx = self.spot.index[self.spot["date"] == trade.entry_time]
            current_idx = self.spot.index[self.spot["date"] == candle_time]
            if len(entry_idx) > 0 and len(current_idx) > 0:
                candles_held = current_idx[0] - entry_idx[0]

            total_move = trade.target - trade.entry_price
            current_move = close - trade.entry_price if trade.action == "BUY" else trade.entry_price - close
            move_pct = (current_move / total_move * 100) if total_move > 0 else 0

            # ── EXIT CHECKS (priority order) ──

            # 1. HARD STOP LOSS — always respected, no delay
            if trade.action == "BUY" and low <= trade.trailing_sl:
                exit_reason = "TRAILING_SL" if trade.trailing_sl > trade.stop_loss else "SL_HIT"
                self._close_trade(trade, trade.trailing_sl, candle_time, exit_reason)
                continue

            # 2. EOD squareoff — before target check (must exit)
            if candle_time.hour == 15 and candle_time.minute >= 15:
                self._close_trade(trade, close, candle_time, "EOD_SQUAREOFF")
                continue

            # 3. PARTIAL PROFIT BOOKING — exit 50% qty at 50% of target
            partial_pct = self.p.get("partial_exit_pct", 50)
            if (not trade.partial_exited and move_pct >= partial_pct
                    and trade.quantity > trade.lot_size):
                # Book half the position
                partial_qty = (trade.quantity // (2 * trade.lot_size)) * trade.lot_size
                if partial_qty >= trade.lot_size:
                    partial_pnl = (close - trade.entry_price) * partial_qty
                    partial_charges = calculate_charges(trade.entry_price, close, partial_qty)
                    trade.pnl += partial_pnl  # accumulate partial P&L
                    trade.charges += partial_charges
                    trade.quantity -= partial_qty
                    trade.partial_exited = True
                    # Move SL to breakeven after partial exit
                    trade.trailing_sl = max(trade.trailing_sl, trade.entry_price)
                    logger.debug(f"Trade #{trade.trade_id}: Partial exit {partial_qty} @ {close:.2f}")

            # 4. FULL TARGET HIT — but respect min hold time
            min_hold = self.p.get("min_hold_candles", 2)
            if trade.action == "BUY" and high >= trade.target and candles_held >= min_hold:
                self._close_trade(trade, trade.target, candle_time, "TARGET_HIT")
                continue

            # 5. TRAILING STOP LOSS — progressive tightening
            if trade.action == "BUY" and total_move > 0:
                breakeven_trigger = self.p.get("breakeven_trigger_pct", 20)

                # Stage 1: Breakeven
                if move_pct >= breakeven_trigger:
                    trade.trailing_sl = max(trade.trailing_sl, trade.entry_price)

                # Stage 2: Lock 30% of profit at 50%
                if move_pct >= 50:
                    new_sl = trade.entry_price + current_move * 0.30
                    trade.trailing_sl = max(trade.trailing_sl, round(new_sl, 2))

                # Stage 3: Lock 50% of profit at 70%
                if move_pct >= 70:
                    new_sl = trade.entry_price + current_move * 0.50
                    trade.trailing_sl = max(trade.trailing_sl, round(new_sl, 2))

                # Stage 4: Lock 70% of profit at 85%
                if move_pct >= 85:
                    new_sl = trade.entry_price + current_move * 0.70
                    trade.trailing_sl = max(trade.trailing_sl, round(new_sl, 2))

            # 6. Max holding time with low profit
            holding_mins = (candle_time - trade.entry_time).total_seconds() / 60
            if holding_mins > self.p["max_holding_minutes"]:
                profit_pct = (current_move / trade.entry_price) * 100
                if profit_pct < 10:
                    self._close_trade(trade, close, candle_time, "MAX_HOLD_TIME")
                    continue

    def _close_trade(self, trade: BacktestTrade, exit_price: float,
                     exit_time: datetime, reason: str):
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_reason = reason
        trade.status = "CLOSED"

        # Calculate P&L on remaining quantity
        if trade.action == "BUY":
            remaining_pnl = (exit_price - trade.entry_price) * trade.quantity
        else:
            remaining_pnl = (trade.entry_price - exit_price) * trade.quantity

        remaining_charges = calculate_charges(trade.entry_price, exit_price, trade.quantity)

        # Add to any accumulated partial P&L
        trade.pnl += remaining_pnl
        trade.charges += remaining_charges
        trade.net_pnl = trade.pnl - trade.charges
        # Restore original qty for reporting
        trade.quantity = trade.original_qty
        self.capital += trade.net_pnl

        # Track daily PnL
        day_str = exit_time.strftime("%Y-%m-%d")
        self._daily_pnl_tracker[day_str] = self._daily_pnl_tracker.get(day_str, 0) + trade.net_pnl

        self._open_trades.remove(trade)
        self._closed_trades.append(trade)
        logger.debug(f"Trade #{trade.trade_id} closed: {reason} "
                      f"P&L={trade.net_pnl:+.2f}")

    def _unrealized_pnl(self, trade: BacktestTrade, spot_price: float) -> float:
        # Rough estimate using entry price (since we may not have current option price)
        return 0  # conservative: don't count unrealized

    def _get_day_pnl(self, day: object) -> float:
        day_str = str(day)
        return self._daily_pnl_tracker.get(day_str, 0)

    def _record_daily_pnl(self, day):
        day_str = str(day)
        if day_str not in self._daily_pnl_tracker:
            self._daily_pnl_tracker[day_str] = 0.0


class MultiBacktestEngine:
    """Run backtests across multiple underlyings and merge results.

    Usage:
        data = generate_multi_synthetic_data(["NIFTY", "BANKNIFTY"], days=60)
        engine = MultiBacktestEngine(data, initial_capital=200000)
        result = engine.run()
    """

    # Per-underlying default overrides (BANKNIFTY needs different tuning)
    UNDERLYING_PARAMS = {
        "BANKNIFTY": {
            "atr_sl_multiplier": 3.0,       # wider SL — BNF 5m range ~40pts, need SL > that
            "min_sl_pct": 0.15,             # floor: SL never less than 15% of premium
            "max_premium": 500,             # avoid deep ITM (high premium, tiny SL%)
            "breakeven_trigger_pct": 20,    # faster breakeven
            "rr_ratio": 1.5,               # grab profits faster
            "max_rsi_ce": 68,              # tighter overbought cap for CE
            "skip_first_minutes": 30,      # skip first 30 min (9:15-9:45)
        },
    }

    def __init__(self, multi_data: dict, initial_capital: float = 200_000,
                 strategies: list[str] = None, params: dict = None):
        """
        Args:
            multi_data: dict mapping underlying -> {"spot": DataFrame, "options": dict}
            initial_capital: total capital (split equally across underlyings)
            strategies: strategy list
            params: override params (applied to all underlyings, then per-underlying overrides on top)
        """
        self.multi_data = multi_data
        self.initial_capital = initial_capital
        self.strategies = strategies
        self.params = params or {}
        self.underlyings = list(multi_data.keys())

    def run(self) -> BacktestResult:
        """Run backtest per underlying, then merge results."""
        n = len(self.underlyings)
        capital_each = self.initial_capital / n

        all_trades = []
        all_equity = []
        all_daily = {}
        trade_id_offset = 0

        for underlying in self.underlyings:
            data = self.multi_data[underlying]
            logger.info(f"Running backtest for {underlying}...")

            # Merge base params + per-underlying overrides
            merged_params = dict(self.params)
            if underlying in self.UNDERLYING_PARAMS:
                merged_params.update(self.UNDERLYING_PARAMS[underlying])

            engine = BacktestEngine(
                spot_data=data["spot"],
                options_data=data["options"],
                underlying=underlying,
                initial_capital=capital_each,
                strategies=self.strategies,
                params=merged_params,
            )
            result = engine.run()

            # Re-number trade IDs to avoid collisions
            for t in result.trades:
                t.trade_id += trade_id_offset
            trade_id_offset += len(result.trades)

            all_trades.extend(result.trades)

            # Merge equity curves (offset by per-underlying capital)
            for e in result.equity_curve:
                all_equity.append(e)

            # Merge daily PnL
            for d in result.daily_pnl:
                day = d["date"]
                all_daily[day] = all_daily.get(day, 0) + d["pnl"]

        # Sort trades by entry time
        all_trades.sort(key=lambda t: t.entry_time)

        # Rebuild merged equity curve from combined trades
        merged_equity = []
        running_capital = self.initial_capital
        for t in all_trades:
            running_capital += t.net_pnl
            merged_equity.append({
                "date": t.exit_time or t.entry_time,
                "equity": running_capital,
                "capital": running_capital,
                "open_positions": 0,
            })

        # If no trades, use the raw equity data
        if not merged_equity and all_equity:
            merged_equity = all_equity

        merged_daily = [{"date": k, "pnl": round(v, 2)}
                        for k, v in sorted(all_daily.items())]

        return BacktestResult(
            trades=all_trades,
            equity_curve=merged_equity,
            daily_pnl=merged_daily,
            params=self.params,
        )
