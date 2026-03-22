"""Fetch real NIFTY spot data from Yahoo Finance (free) and derive option prices."""
from __future__ import annotations

import logging
import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm
from config.instruments import UNDERLYING_MAP

logger = logging.getLogger(__name__)

YAHOO_SYMBOLS = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}


def fetch_real_spot(underlying: str, start: str, end: str,
                    interval: str = "5m") -> pd.DataFrame:
    """Fetch real OHLCV data from Yahoo Finance.

    Args:
        underlying: 'NIFTY' or 'BANKNIFTY'
        start: 'YYYY-MM-DD'
        end: 'YYYY-MM-DD'
        interval: '1m', '5m', '15m', '1d' etc.
            Note: 1m only available for last 7 days
                  5m/15m available for last 60 days
                  1d available for years

    Returns DataFrame with: date, open, high, low, close, volume
    """
    symbol = YAHOO_SYMBOLS.get(underlying)
    if not symbol:
        raise ValueError(f"Unknown underlying: {underlying}")

    logger.info(f"Fetching {underlying} ({symbol}) from Yahoo Finance: {start} to {end} ({interval})")
    ticker = yf.Ticker(symbol)
    df = ticker.history(start=start, end=end, interval=interval)

    if df.empty:
        logger.error(f"No data returned for {symbol} {start}-{end}")
        return pd.DataFrame()

    # Normalize columns
    df = df.reset_index()
    if "Datetime" in df.columns:
        df = df.rename(columns={"Datetime": "date"})
    elif "Date" in df.columns:
        df = df.rename(columns={"Date": "date"})

    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume"
    })

    # Remove timezone info for consistency
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)

    # Keep only market hours (9:15 - 15:30 IST)
    if interval != "1d":
        df = df[(df["date"].dt.hour >= 9) & (df["date"].dt.hour < 16)]
        df = df[~((df["date"].dt.hour == 9) & (df["date"].dt.minute < 15))]

    df = df[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    logger.info(f"Fetched {len(df)} candles, {df['date'].dt.date.nunique()} trading days")
    return df


def derive_option_chain(spot_df: pd.DataFrame, underlying: str = "NIFTY",
                         n_strikes: int = 10, risk_free: float = 0.065,
                         base_iv: float = 0.15) -> dict:
    """Derive realistic option prices from real spot data using Black-Scholes.

    This gives us REAL price action driving option premiums, which is far
    more accurate than fully synthetic data. OI and volume are still estimated
    but option price movements will track real NIFTY moves.

    Returns dict mapping tradingsymbol -> DataFrame
    """
    strike_step = UNDERLYING_MAP[underlying]["strike_step"]
    lot_size = 75 if underlying == "NIFTY" else 30

    # Determine strike range from actual spot range
    spot_min = spot_df["close"].min()
    spot_max = spot_df["close"].max()
    spot_mid = (spot_min + spot_max) / 2
    atm = round(spot_mid / strike_step) * strike_step
    extra = max(n_strikes, int((spot_max - spot_min) / strike_step) + 3)
    strikes = [atm + i * strike_step for i in range(-extra, extra + 1)]

    # Estimate days to expiry (assume weekly expiry, Thursday)
    dates = spot_df["date"].dt.date.unique()

    np.random.seed(99)  # reproducible OI/volume
    options = {}

    for strike in strikes:
        for opt_type in ("CE", "PE"):
            sym = f"{underlying}{int(strike)}{opt_type}"
            records = []

            for _, row in spot_df.iterrows():
                S = row["close"]
                K = strike
                # Estimate time to expiry (days until next Thursday)
                dt = row["date"]
                days_to_thu = (3 - dt.weekday()) % 7
                if days_to_thu == 0 and dt.hour >= 15:
                    days_to_thu = 7
                T = max(days_to_thu, 0.5) / 365.0

                # Add some IV variation based on moneyness and time
                moneyness = abs(S - K) / S
                iv = base_iv + moneyness * 0.3 + np.random.normal(0, 0.01)
                iv = max(iv, 0.08)  # floor

                # Black-Scholes price
                price = _bs_price(S, K, T, risk_free, iv, opt_type)
                price = max(price, 0.5)  # minimum premium

                # Realistic high/low from price
                intraday_vol = price * 0.03  # ~3% intraday range
                high = price + abs(np.random.normal(0, intraday_vol))
                low = price - abs(np.random.normal(0, intraday_vol))
                low = max(low, 0.5)

                # OI: higher near ATM, lower far OTM
                atm_distance = abs(S - K) / strike_step
                oi_base = max(50000, 2000000 * np.exp(-atm_distance * 0.3))
                oi = int(oi_base * np.random.uniform(0.7, 1.3))

                # Volume: correlated with OI and spot volume
                vol_ratio = row["volume"] / max(spot_df["volume"].mean(), 1)
                volume = int(oi * 0.1 * vol_ratio * np.random.uniform(0.5, 1.5))
                volume = max(volume, 100)

                records.append({
                    "date": row["date"],
                    "open": round(price * np.random.uniform(0.99, 1.01), 2),
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "close": round(price, 2),
                    "volume": volume,
                    "oi": oi,
                    "strike": float(K),
                    "instrument_type": opt_type,
                    "tradingsymbol": sym,
                    "lot_size": lot_size,
                })

            options[sym] = pd.DataFrame(records)

    logger.info(f"Derived {len(options)} option contracts from real spot data")
    return options


def _bs_price(S, K, T, r, sigma, opt_type):
    """Black-Scholes option price."""
    if T <= 0 or sigma <= 0:
        if opt_type == "CE":
            return max(S - K, 0)
        return max(K - S, 0)

    d1 = (np.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if opt_type == "CE":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def fetch_real_data(underlying: str = "NIFTY", start: str = "2026-01-01",
                    end: str = "2026-01-31", interval: str = "5m") -> dict:
    """One-call function: fetch real spot + derive options.

    Returns {"spot": DataFrame, "options": dict}
    """
    spot = fetch_real_spot(underlying, start, end, interval)
    if spot.empty:
        return {"spot": spot, "options": {}}

    options = derive_option_chain(spot, underlying)
    return {"spot": spot, "options": options}
