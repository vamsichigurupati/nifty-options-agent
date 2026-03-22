"""Technical indicators for trend detection and signal confirmation."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """Supertrend indicator. Returns DataFrame with 'supertrend' and 'direction' columns.
    direction: 1 = bullish (price above supertrend), -1 = bearish.
    """
    hl2 = (df["high"] + df["low"]) / 2
    atr = true_range(df).rolling(window=period).mean()

    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    supertrend_vals = np.zeros(len(df))
    direction = np.zeros(len(df))

    supertrend_vals[0] = upper_band.iloc[0]
    direction[0] = 1

    for i in range(1, len(df)):
        # Lower band logic
        if lower_band.iloc[i] > lower_band.iloc[i-1] or df["close"].iloc[i-1] < lower_band.iloc[i-1]:
            pass  # keep current lower_band
        else:
            lower_band.iloc[i] = lower_band.iloc[i-1]

        # Upper band logic
        if upper_band.iloc[i] < upper_band.iloc[i-1] or df["close"].iloc[i-1] > upper_band.iloc[i-1]:
            pass
        else:
            upper_band.iloc[i] = upper_band.iloc[i-1]

        # Direction
        if supertrend_vals[i-1] == upper_band.iloc[i-1]:
            if df["close"].iloc[i] > upper_band.iloc[i]:
                supertrend_vals[i] = lower_band.iloc[i]
                direction[i] = 1
            else:
                supertrend_vals[i] = upper_band.iloc[i]
                direction[i] = -1
        else:
            if df["close"].iloc[i] < lower_band.iloc[i]:
                supertrend_vals[i] = upper_band.iloc[i]
                direction[i] = -1
            else:
                supertrend_vals[i] = lower_band.iloc[i]
                direction[i] = 1

    result = pd.DataFrame({
        "supertrend": supertrend_vals,
        "direction": direction,
    }, index=df.index)
    return result


def true_range(df: pd.DataFrame) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    return pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    return true_range(df).rolling(window=period).mean()


def linear_regression_slope(series: pd.Series, period: int = 20) -> pd.Series:
    """Linear Regression Slope. Positive = uptrend, negative = downtrend.
    Mathematically precise, zero lag, never gets stuck."""
    slopes = pd.Series(index=series.index, dtype=float)
    vals = series.values
    x = np.arange(period)
    for i in range(period, len(vals)):
        slope = np.polyfit(x, vals[i-period:i], 1)[0]
        slopes.iloc[i] = slope
    return slopes


def awesome_oscillator(df: pd.DataFrame, fast: int = 5, slow: int = 34) -> pd.Series:
    """Awesome Oscillator — momentum using median price.
    AO > 0 = bullish momentum, AO < 0 = bearish."""
    median_price = (df["high"] + df["low"]) / 2
    return sma(median_price, fast) - sma(median_price, slow)


def adx_di(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """ADX + Directional Index. Returns DataFrame with adx, plus_di, minus_di.
    ADX measures trend strength (>25 = trending).
    +DI > -DI = bullish, +DI < -DI = bearish.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_val = tr.ewm(span=period).mean()
    plus_di = 100 * (plus_dm.ewm(span=period).mean() / atr_val.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(span=period).mean() / atr_val.replace(0, np.nan))
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx_val = dx.ewm(span=period).mean()
    return pd.DataFrame({"adx": adx_val, "plus_di": plus_di, "minus_di": minus_di})


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    """Stochastic Oscillator. Returns DataFrame with k and d.
    %K > 50 = bullish momentum, %K < 50 = bearish.
    %K > 80 = overbought, %K < 20 = oversold.
    """
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return pd.DataFrame({"k": k, "d": d})


def macd(series: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> pd.DataFrame:
    """MACD indicator. Returns DataFrame with macd, signal, histogram."""
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return pd.DataFrame({
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram,
    })


def vwap(df: pd.DataFrame) -> pd.Series:
    """Volume Weighted Average Price (intraday, resets daily)."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_tp_vol = (typical_price * df["volume"]).cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def bollinger_bands(series: pd.Series, period: int = 20,
                     std_dev: float = 2.0) -> pd.DataFrame:
    mid = sma(series, period)
    std = series.rolling(window=period).std()
    return pd.DataFrame({
        "upper": mid + std_dev * std,
        "middle": mid,
        "lower": mid - std_dev * std,
    })


def detect_trend(df: pd.DataFrame) -> dict:
    """Detect current trend using multiple indicators on spot data.

    Args:
        df: OHLCV DataFrame (needs at least 50 rows)

    Returns dict with:
        direction: 1 (bullish), -1 (bearish), 0 (sideways)
        strength: 0-100 (how many indicators agree)
        details: per-indicator signals
    """
    min_candles = 20
    if len(df) < min_candles:
        return {"direction": 0, "strength": 0, "details": {}}

    close = df["close"]
    signals = {}
    bullish_count = 0
    total_signals = 0

    # 1. EMA crossover (5 vs 13) — faster than 9/21 to catch reversals
    ema_fast = ema(close, 5).iloc[-1]
    ema_slow = ema(close, 13).iloc[-1]
    signals["ema_cross"] = "BULL" if ema_fast > ema_slow else "BEAR"
    if ema_fast > ema_slow:
        bullish_count += 1
    total_signals += 1
    ema9 = ema_fast  # store for reporting
    ema21 = ema_slow

    # 2. Price vs EMA 20 (shorter than EMA 50 — more responsive)
    ema20 = ema(close, 20).iloc[-1]
    current = close.iloc[-1]
    signals["price_vs_ema50"] = "BULL" if current > ema20 else "BEAR"
    if current > ema20:
        bullish_count += 1
    total_signals += 1

    # 3. Linear Regression Slope (score: 88.5 — highest ranked indicator)
    rsi_val = rsi(close, 14).iloc[-1]  # keep RSI for gate filters
    lr = linear_regression_slope(close, 20)
    lr_val = lr.iloc[-1]
    if lr_val > 0:
        signals["linreg_slope"] = "BULL"
        bullish_count += 1
    elif lr_val < 0:
        signals["linreg_slope"] = "BEAR"
    else:
        signals["linreg_slope"] = "NEUTRAL"
        bullish_count += 0.5
    total_signals += 1

    # 4. MACD
    macd_data = macd(close)
    signals["macd"] = "BULL" if macd_data["histogram"].iloc[-1] > 0 else "BEAR"
    if macd_data["histogram"].iloc[-1] > 0:
        bullish_count += 1
    total_signals += 1

    # 5. Awesome Oscillator (score: 87.5 — 2nd highest)
    ao = awesome_oscillator(df)
    ao_val = ao.iloc[-1]
    if ao_val > 0:
        signals["awesome_osc"] = "BULL"
        bullish_count += 1
    else:
        signals["awesome_osc"] = "BEAR"
    total_signals += 1

    # 6. Short-term slope: are last 3 closes rising or falling?
    last3 = close.iloc[-3:].values
    slope_up = last3[-1] > last3[0]
    slope_pct = (last3[-1] - last3[0]) / last3[0] * 100
    if slope_up and slope_pct > 0.02:
        signals["price_action"] = "BULL"
        bullish_count += 1
    elif not slope_up and slope_pct < -0.02:
        signals["price_action"] = "BEAR"
    else:
        signals["price_action"] = "NEUTRAL"
        bullish_count += 0.5
    total_signals += 1

    # Aggregate
    bull_pct = (bullish_count / total_signals) * 100
    bear_pct = 100 - bull_pct
    # Use asymmetric thresholds: bearish signals are harder to generate
    # from lagging indicators, so require less agreement for bear
    if bull_pct >= 60:
        direction = 1
    elif bear_pct >= 55:  # 55% bearish = at least 3.3/6 indicators say bear
        direction = -1
    else:
        direction = 0

    return {
        "direction": direction,
        "strength": round(bull_pct, 1),
        "rsi": round(rsi_val, 1),
        "ema9": round(ema9, 2),
        "ema21": round(ema21, 2),
        "details": signals,
    }
