"""Fetch and cache historical data from Kite Connect for backtesting."""

import os
import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
import pandas as pd
from config.instruments import UNDERLYING_MAP

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent / "data" / "historical"


class HistoricalDataFetcher:
    def __init__(self, kite):
        self.kite = kite
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, symbol: str, from_date: str, to_date: str,
                    interval: str) -> Path:
        safe = symbol.replace(":", "_").replace(" ", "_")
        return CACHE_DIR / f"{safe}_{from_date}_{to_date}_{interval}.parquet"

    # ── Spot / Index Historical Data ──────────────────────────

    def get_spot_history(self, underlying: str, from_date: str,
                         to_date: str, interval: str = "5minute") -> pd.DataFrame:
        """Fetch OHLCV history for NIFTY/BANKNIFTY spot.

        Args:
            underlying: 'NIFTY' or 'BANKNIFTY'
            from_date: 'YYYY-MM-DD'
            to_date: 'YYYY-MM-DD'
            interval: 'minute', '5minute', '15minute', '60minute', 'day'

        Returns DataFrame with columns: date, open, high, low, close, volume
        """
        info = UNDERLYING_MAP[underlying]
        token = info["spot_token"]
        cache = self._cache_path(underlying, from_date, to_date, interval)

        if cache.exists():
            logger.info(f"Loading cached spot data: {cache.name}")
            return pd.read_parquet(cache)

        logger.info(f"Fetching {underlying} spot {from_date} to {to_date} ({interval})")
        records = self._fetch_chunked(token, from_date, to_date, interval)
        df = pd.DataFrame(records)
        if df.empty:
            return df

        df["date"] = pd.to_datetime(df["date"])
        df.to_parquet(cache)
        return df

    def _fetch_chunked(self, token: int, from_date: str, to_date: str,
                       interval: str) -> list[dict]:
        """Kite limits historical data to 60-day chunks for intraday. Split accordingly."""
        start = datetime.strptime(from_date, "%Y-%m-%d").date()
        end = datetime.strptime(to_date, "%Y-%m-%d").date()
        chunk_days = 55 if "minute" in interval else 365
        all_records = []

        current = start
        while current <= end:
            chunk_end = min(current + timedelta(days=chunk_days), end)
            try:
                records = self.kite.historical_data(
                    token, current, chunk_end, interval
                )
                all_records.extend(records)
            except Exception as e:
                logger.error(f"Historical fetch error {current}-{chunk_end}: {e}")
            current = chunk_end + timedelta(days=1)

        return all_records

    # ── Option Chain Snapshots ────────────────────────────────

    def get_option_history(self, instrument_token: int, from_date: str,
                           to_date: str, interval: str = "5minute") -> pd.DataFrame:
        """Fetch OHLCV history for a specific option contract."""
        cache = self._cache_path(
            f"OPT_{instrument_token}", from_date, to_date, interval
        )
        if cache.exists():
            return pd.read_parquet(cache)

        records = self._fetch_chunked(instrument_token, from_date, to_date, interval)
        df = pd.DataFrame(records)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df.to_parquet(cache)
        return df

    # ── Build option chain for a date range ───────────────────

    def get_instruments_for_expiry(self, underlying: str,
                                    expiry: date) -> list[dict]:
        """Get all option instruments for an underlying + expiry from NFO list."""
        instruments = self.kite.instruments("NFO")
        prefix = UNDERLYING_MAP[underlying]["nfo_prefix"]

        return [i for i in instruments
                if i["name"] == prefix
                and i["instrument_type"] in ("CE", "PE")
                and i["segment"] == "NFO-OPT"
                and i["expiry"] == expiry]

    def build_option_chain_history(self, underlying: str, expiry: str,
                                    from_date: str, to_date: str,
                                    strikes: list[float] = None,
                                    interval: str = "5minute") -> dict[str, pd.DataFrame]:
        """Fetch historical data for all options in a chain.

        Returns dict mapping tradingsymbol -> DataFrame.
        This can be SLOW (many API calls). Use narrow strike ranges.
        """
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        instruments = self.get_instruments_for_expiry(underlying, expiry_date)

        if strikes:
            instruments = [i for i in instruments if i["strike"] in strikes]

        chain = {}
        total = len(instruments)
        for idx, inst in enumerate(instruments):
            sym = inst["tradingsymbol"]
            logger.info(f"Fetching {sym} ({idx+1}/{total})")
            try:
                df = self.get_option_history(
                    inst["instrument_token"], from_date, to_date, interval
                )
                if not df.empty:
                    df["strike"] = inst["strike"]
                    df["instrument_type"] = inst["instrument_type"]
                    df["tradingsymbol"] = sym
                    df["lot_size"] = inst["lot_size"]
                    chain[sym] = df
            except Exception as e:
                logger.error(f"Failed to fetch {sym}: {e}")
        return chain


def generate_multi_synthetic_data(underlyings: list = None, days: int = 30,
                                   interval_minutes: int = 5) -> dict:
    """Generate synthetic data for multiple underlyings.

    Returns dict mapping underlying -> {"spot": DataFrame, "options": dict}
    """
    if underlyings is None:
        underlyings = ["NIFTY"]

    defaults = {"NIFTY": 22500, "BANKNIFTY": 48000}
    result = {}
    for underlying in underlyings:
        spot_start = defaults.get(underlying, 22500)
        # Use different seed per underlying for independent price paths
        seed = 42 if underlying == "NIFTY" else 123
        result[underlying] = generate_synthetic_data(
            underlying, days, spot_start, interval_minutes, seed=seed
        )
    return result


def generate_synthetic_data(underlying: str = "NIFTY", days: int = 30,
                             spot_start: float = 22500,
                             interval_minutes: int = 5,
                             seed: int = 42) -> dict:
    """Generate synthetic spot + option data for backtesting without Kite API.

    Useful for testing the backtest engine before going live.
    Returns dict with 'spot' DataFrame and 'options' dict of DataFrames.
    """
    import numpy as np

    np.random.seed(seed)
    records = []
    spot = spot_start
    strike_step = UNDERLYING_MAP[underlying]["strike_step"]

    start_date = datetime.now().date() - timedelta(days=days)

    # Create realistic trending/mean-reverting regimes instead of pure random walk
    # Each day has a regime: trending up, trending down, or sideways
    regime_cycle = [1, 1, -1, 0, 1, -1, -1, 1, 0, 1]  # alternating trends
    day_count = 0

    for day_offset in range(days):
        current_date = start_date + timedelta(days=day_offset)
        if current_date.weekday() >= 5:
            continue

        regime = regime_cycle[day_count % len(regime_cycle)]  # 1=up, -1=down, 0=sideways
        day_count += 1
        drift = regime * spot * 0.0003  # directional bias per candle

        candle_time = datetime.combine(current_date, datetime.strptime("09:15", "%H:%M").time())
        end_time = datetime.combine(current_date, datetime.strptime("15:30", "%H:%M").time())

        while candle_time <= end_time:
            noise = np.random.normal(0, spot * 0.0008)
            change = drift + noise
            spot += change
            high = spot + abs(np.random.normal(0, spot * 0.0005))
            low = spot - abs(np.random.normal(0, spot * 0.0005))

            records.append({
                "date": candle_time,
                "open": round(spot - change/2, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(spot, 2),
                "volume": int(np.random.uniform(50000, 500000)),
            })
            candle_time += timedelta(minutes=interval_minutes)

    spot_df = pd.DataFrame(records)

    # Generate synthetic option data
    # Use strikes around the AVERAGE spot price (not just start) to cover full range
    options = {}
    spot_min = spot_df["close"].min()
    spot_max = spot_df["close"].max()
    spot_mid = (spot_min + spot_max) / 2
    atm = round(spot_mid / strike_step) * strike_step
    # Wider strike range to cover price drift
    n_strikes = max(10, int((spot_max - spot_min) / strike_step) + 5)
    strikes = [atm + i * strike_step for i in range(-n_strikes, n_strikes + 1)]

    # Fake expiry every Thursday
    from datetime import timedelta as td
    for strike in strikes:
        for opt_type in ("CE", "PE"):
            sym = f"{underlying}{strike}{opt_type}"
            opt_records = []
            for _, row in spot_df.iterrows():
                spot_now = row["close"]
                # Simplified option pricing
                intrinsic = max(spot_now - strike, 0) if opt_type == "CE" else max(strike - spot_now, 0)
                time_value = max(20, abs(spot_now - strike) * 0.1 * np.random.uniform(0.5, 1.5))
                premium = round(intrinsic + time_value, 2)

                opt_records.append({
                    "date": row["date"],
                    "open": premium * np.random.uniform(0.98, 1.02),
                    "high": premium * np.random.uniform(1.0, 1.05),
                    "low": premium * np.random.uniform(0.95, 1.0),
                    "close": premium,
                    "volume": int(np.random.uniform(1000, 100000)),
                    "oi": int(np.random.uniform(50000, 2000000)),
                    "strike": strike,
                    "instrument_type": opt_type,
                    "tradingsymbol": sym,
                    "lot_size": 75 if underlying == "NIFTY" else 30,
                })
            options[sym] = pd.DataFrame(opt_records)

    return {"spot": spot_df, "options": options}
