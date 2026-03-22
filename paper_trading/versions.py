"""Three strategy versions for paper trading comparison."""
from __future__ import annotations


def get_detect_trend_v1():
    """V1: Original — Supertrend + RSI voter."""
    from strategy.indicators import (ema, rsi, macd, supertrend)

    def detect_trend(df):
        if len(df) < 20:
            return {"direction": 0, "strength": 0, "details": {}, "rsi": 50}
        close = df["close"]
        signals = {}
        bullish_count = 0
        total_signals = 0

        ema_fast = ema(close, 5).iloc[-1]
        ema_slow = ema(close, 13).iloc[-1]
        signals["ema_cross"] = "BULL" if ema_fast > ema_slow else "BEAR"
        if ema_fast > ema_slow: bullish_count += 1
        total_signals += 1

        ema20 = ema(close, 20).iloc[-1]
        current = close.iloc[-1]
        signals["price_vs_ema50"] = "BULL" if current > ema20 else "BEAR"
        if current > ema20: bullish_count += 1
        total_signals += 1

        rsi_val = rsi(close, 14).iloc[-1]
        if rsi_val > 55:
            signals["rsi"] = "BULL"; bullish_count += 1
        elif rsi_val < 45:
            signals["rsi"] = "BEAR"
        else:
            signals["rsi"] = "NEUTRAL"; bullish_count += 0.5
        total_signals += 1

        macd_data = macd(close)
        signals["macd"] = "BULL" if macd_data["histogram"].iloc[-1] > 0 else "BEAR"
        if macd_data["histogram"].iloc[-1] > 0: bullish_count += 1
        total_signals += 1

        st = supertrend(df, period=7, multiplier=2.5)
        signals["supertrend"] = "BULL" if st["direction"].iloc[-1] == 1 else "BEAR"
        if st["direction"].iloc[-1] == 1: bullish_count += 1
        total_signals += 1

        last3 = close.iloc[-3:].values
        slope_pct = (last3[-1] - last3[0]) / last3[0] * 100
        if last3[-1] > last3[0] and slope_pct > 0.02:
            signals["price_action"] = "BULL"; bullish_count += 1
        elif last3[-1] < last3[0] and slope_pct < -0.02:
            signals["price_action"] = "BEAR"
        else:
            signals["price_action"] = "NEUTRAL"; bullish_count += 0.5
        total_signals += 1

        bull_pct = (bullish_count / total_signals) * 100
        bear_pct = 100 - bull_pct
        if bull_pct >= 60: direction = 1
        elif bear_pct >= 55: direction = -1
        else: direction = 0

        return {"direction": direction, "strength": round(bull_pct, 1),
                "rsi": round(rsi_val, 1), "ema9": round(ema_fast, 2),
                "ema21": round(ema_slow, 2), "details": signals}
    return detect_trend


def get_detect_trend_v2():
    """V2: ADX+DI + Stochastic."""
    from strategy.indicators import (ema, rsi, macd, adx_di, stochastic)

    def detect_trend(df):
        if len(df) < 20:
            return {"direction": 0, "strength": 0, "details": {}, "rsi": 50}
        close = df["close"]
        signals = {}
        bullish_count = 0
        total_signals = 0

        ema_fast = ema(close, 5).iloc[-1]
        ema_slow = ema(close, 13).iloc[-1]
        signals["ema_cross"] = "BULL" if ema_fast > ema_slow else "BEAR"
        if ema_fast > ema_slow: bullish_count += 1
        total_signals += 1

        ema20 = ema(close, 20).iloc[-1]
        current = close.iloc[-1]
        signals["price_vs_ema50"] = "BULL" if current > ema20 else "BEAR"
        if current > ema20: bullish_count += 1
        total_signals += 1

        rsi_val = rsi(close, 14).iloc[-1]
        stoch_data = stochastic(df, k_period=14, d_period=3)
        stoch_k = stoch_data["k"].iloc[-1]
        if stoch_k > 50:
            signals["stochastic"] = "BULL"; bullish_count += 1
        else:
            signals["stochastic"] = "BEAR"
        total_signals += 1

        macd_data = macd(close)
        signals["macd"] = "BULL" if macd_data["histogram"].iloc[-1] > 0 else "BEAR"
        if macd_data["histogram"].iloc[-1] > 0: bullish_count += 1
        total_signals += 1

        adx_data = adx_di(df, period=14)
        if adx_data["plus_di"].iloc[-1] > adx_data["minus_di"].iloc[-1]:
            signals["adx_di"] = "BULL"; bullish_count += 1
        else:
            signals["adx_di"] = "BEAR"
        total_signals += 1

        last3 = close.iloc[-3:].values
        slope_pct = (last3[-1] - last3[0]) / last3[0] * 100
        if last3[-1] > last3[0] and slope_pct > 0.02:
            signals["price_action"] = "BULL"; bullish_count += 1
        elif last3[-1] < last3[0] and slope_pct < -0.02:
            signals["price_action"] = "BEAR"
        else:
            signals["price_action"] = "NEUTRAL"; bullish_count += 0.5
        total_signals += 1

        bull_pct = (bullish_count / total_signals) * 100
        bear_pct = 100 - bull_pct
        if bull_pct >= 60: direction = 1
        elif bear_pct >= 55: direction = -1
        else: direction = 0

        return {"direction": direction, "strength": round(bull_pct, 1),
                "rsi": round(rsi_val, 1), "ema9": round(ema_fast, 2),
                "ema21": round(ema_slow, 2), "details": signals}
    return detect_trend


def get_detect_trend_v3():
    """V3: LinReg Slope + Awesome Oscillator."""
    from strategy.indicators import (ema, rsi, macd, linear_regression_slope,
                                      awesome_oscillator)

    def detect_trend(df):
        if len(df) < 20:
            return {"direction": 0, "strength": 0, "details": {}, "rsi": 50}
        close = df["close"]
        signals = {}
        bullish_count = 0
        total_signals = 0

        ema_fast = ema(close, 5).iloc[-1]
        ema_slow = ema(close, 13).iloc[-1]
        signals["ema_cross"] = "BULL" if ema_fast > ema_slow else "BEAR"
        if ema_fast > ema_slow: bullish_count += 1
        total_signals += 1

        ema20 = ema(close, 20).iloc[-1]
        current = close.iloc[-1]
        signals["price_vs_ema50"] = "BULL" if current > ema20 else "BEAR"
        if current > ema20: bullish_count += 1
        total_signals += 1

        rsi_val = rsi(close, 14).iloc[-1]
        lr = linear_regression_slope(close, 20)
        lr_val = lr.iloc[-1]
        if lr_val > 0:
            signals["linreg_slope"] = "BULL"; bullish_count += 1
        elif lr_val < 0:
            signals["linreg_slope"] = "BEAR"
        else:
            signals["linreg_slope"] = "NEUTRAL"; bullish_count += 0.5
        total_signals += 1

        macd_data = macd(close)
        signals["macd"] = "BULL" if macd_data["histogram"].iloc[-1] > 0 else "BEAR"
        if macd_data["histogram"].iloc[-1] > 0: bullish_count += 1
        total_signals += 1

        ao = awesome_oscillator(df)
        ao_val = ao.iloc[-1]
        if ao_val > 0:
            signals["awesome_osc"] = "BULL"; bullish_count += 1
        else:
            signals["awesome_osc"] = "BEAR"
        total_signals += 1

        last3 = close.iloc[-3:].values
        slope_pct = (last3[-1] - last3[0]) / last3[0] * 100
        if last3[-1] > last3[0] and slope_pct > 0.02:
            signals["price_action"] = "BULL"; bullish_count += 1
        elif last3[-1] < last3[0] and slope_pct < -0.02:
            signals["price_action"] = "BEAR"
        else:
            signals["price_action"] = "NEUTRAL"; bullish_count += 0.5
        total_signals += 1

        bull_pct = (bullish_count / total_signals) * 100
        bear_pct = 100 - bull_pct
        if bull_pct >= 60: direction = 1
        elif bear_pct >= 55: direction = -1
        else: direction = 0

        return {"direction": direction, "strength": round(bull_pct, 1),
                "rsi": round(rsi_val, 1), "ema9": round(ema_fast, 2),
                "ema21": round(ema_slow, 2), "details": signals}
    return detect_trend


VERSION_CONFIG = {
    "V1_SupertrendRSI": {
        "name": "V1: Supertrend + RSI",
        "detect_trend": get_detect_trend_v1,
        "color": "#3b82f6",
    },
    "V3_LinRegAwesome": {
        "name": "V3: LinReg + Awesome Osc",
        "detect_trend": get_detect_trend_v3,
        "color": "#10b981",
    },
}
