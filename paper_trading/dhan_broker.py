"""Dhan broker integration for paper trading with real market data."""
from __future__ import annotations

import logging
import os
from datetime import datetime, date, timedelta
import pandas as pd
from dhanhq import dhanhq

logger = logging.getLogger(__name__)

# Dhan security IDs for indices
NIFTY_SECURITY_ID = "13"      # NIFTY 50 index
BANKNIFTY_SECURITY_ID = "25"  # BANK NIFTY index


class DhanBroker:
    """Wrapper around DhanHQ SDK for data fetching and virtual order placement."""

    def __init__(self, client_id: str = None, access_token: str = None):
        self.client_id = client_id or os.getenv("DHAN_CLIENT_ID", "")
        self.access_token = access_token or os.getenv("DHAN_ACCESS_TOKEN", "")

        if not self.client_id or not self.access_token:
            raise ValueError(
                "Dhan credentials required. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN "
                "in .env or pass directly.\n"
                "Get free API access at: https://dhanhq.co"
            )

        self.dhan = dhanhq(self.client_id, self.access_token)
        self._security_cache = None
        self._cache_date = None
        logger.info("Dhan broker initialized")

    # ── Market Data ──────────────────────────────────────────

    def get_spot_price(self, underlying: str = "NIFTY") -> float:
        """Get current spot price of NIFTY/BANKNIFTY."""
        sec_id = NIFTY_SECURITY_ID if underlying == "NIFTY" else BANKNIFTY_SECURITY_ID
        data = self.dhan.ticker_data(
            security_id=sec_id,
            exchange_segment=self.dhan.INDEX
        )
        if data and data.get("status") == "success":
            return data["data"]["LTP"]
        raise RuntimeError(f"Failed to get spot price: {data}")

    def get_intraday_data(self, security_id: str, exchange: str,
                           from_date: str, to_date: str) -> pd.DataFrame:
        """Get intraday 5-min OHLCV data."""
        data = self.dhan.intraday_minute_data(
            security_id=security_id,
            exchange_segment=exchange,
            instrument_type="INDEX",
            from_date=from_date,
            to_date=to_date,
        )
        if not data or data.get("status") != "success":
            return pd.DataFrame()

        candles = data.get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()

        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["timestamp"])
        return df[["date", "open", "high", "low", "close", "volume"]]

    def get_nifty_spot_data(self, days: int = 5) -> pd.DataFrame:
        """Get recent NIFTY intraday data."""
        today = date.today()
        from_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        to_date = today.strftime("%Y-%m-%d")
        return self.get_intraday_data(
            NIFTY_SECURITY_ID, self.dhan.INDEX, from_date, to_date
        )

    def get_option_chain(self, underlying: str = "NIFTY",
                          expiry: str = None) -> list[dict]:
        """Get live option chain with LTP, OI, Greeks."""
        sec_id = NIFTY_SECURITY_ID if underlying == "NIFTY" else BANKNIFTY_SECURITY_ID
        params = {"UnderlyingScrip": int(sec_id), "ExpiryDate": expiry} if expiry else {}

        data = self.dhan.option_chain(
            under_security_id=int(sec_id),
            expiry=expiry or "",
        )
        if not data or data.get("status") != "success":
            return []

        return data.get("data", [])

    def get_option_ltp(self, security_id: str) -> dict:
        """Get LTP and quote for a specific option contract."""
        data = self.dhan.ticker_data(
            security_id=security_id,
            exchange_segment=self.dhan.NSE_FNO
        )
        if data and data.get("status") == "success":
            return data["data"]
        return {}

    def get_quote(self, security_id: str) -> dict:
        """Get full quote with OI, volume, depth."""
        data = self.dhan.quote_data(
            security_id=security_id,
            exchange_segment=self.dhan.NSE_FNO
        )
        if data and data.get("status") == "success":
            return data["data"]
        return {}

    # ── Expiry Management ────────────────────────────────────

    def get_nearest_expiry(self, underlying: str = "NIFTY") -> str:
        """Get nearest weekly expiry date."""
        sec_id = NIFTY_SECURITY_ID if underlying == "NIFTY" else BANKNIFTY_SECURITY_ID
        data = self.dhan.expiry_list(
            under_security_id=int(sec_id),
            segment="IDX_I"
        )
        if data and data.get("status") == "success":
            expiries = data.get("data", [])
            if expiries:
                return expiries[0]  # nearest expiry
        return ""

    # ── Order Placement (Virtual/Paper) ──────────────────────

    def place_virtual_order(self, security_id: str, transaction_type: str,
                             quantity: int, order_type: str = "MARKET",
                             price: float = 0, trigger_price: float = 0,
                             product_type: str = "INTRA") -> dict:
        """Place a virtual/paper order on Dhan.

        For paper trading, use the same place_order API —
        Dhan virtual trading uses the same endpoint, just the account is virtual.
        """
        txn = self.dhan.BUY if transaction_type == "BUY" else self.dhan.SELL

        order_params = {
            "security_id": security_id,
            "exchange_segment": self.dhan.NSE_FNO,
            "transaction_type": txn,
            "quantity": quantity,
            "order_type": self.dhan.MARKET if order_type == "MARKET" else self.dhan.LIMIT,
            "product_type": self.dhan.INTRA,
            "price": price,
            "trigger_price": trigger_price,
        }

        try:
            result = self.dhan.place_order(**order_params)
            logger.info(f"Order placed: {transaction_type} {quantity} {security_id} -> {result}")
            return result
        except Exception as e:
            logger.error(f"Order failed: {e}")
            return {"status": "error", "message": str(e)}

    def get_positions(self) -> list[dict]:
        """Get all open positions."""
        data = self.dhan.get_positions()
        if data and data.get("status") == "success":
            return data.get("data", [])
        return []

    def get_order_list(self) -> list[dict]:
        """Get today's order list."""
        data = self.dhan.get_order_list()
        if data and data.get("status") == "success":
            return data.get("data", [])
        return []

    # ── Security List ────────────────────────────────────────

    def fetch_security_list(self) -> pd.DataFrame:
        """Download and cache Dhan security master list."""
        today = date.today()
        if self._security_cache is not None and self._cache_date == today:
            return self._security_cache

        try:
            # Dhan provides a CSV of all securities
            self.dhan.fetch_security_list(exchange="NSE_FNO")
            # The SDK saves it locally, load it
            import glob
            files = glob.glob("api-scrip-master-*.csv") + glob.glob("scrip-master*.csv")
            if files:
                df = pd.read_csv(files[0])
                self._security_cache = df
                self._cache_date = today
                return df
        except Exception as e:
            logger.error(f"Failed to fetch security list: {e}")
        return pd.DataFrame()

    def find_option_security_id(self, underlying: str, strike: float,
                                  option_type: str, expiry: str = None) -> str:
        """Find security ID for a specific option contract."""
        sec_list = self.fetch_security_list()
        if sec_list.empty:
            return ""

        name_prefix = "NIFTY" if underlying == "NIFTY" else "BANKNIFTY"
        mask = (
            sec_list["SEM_TRADING_SYMBOL"].str.startswith(name_prefix) &
            sec_list["SEM_STRIKE_PRICE"].astype(float).eq(strike) &
            sec_list["SEM_OPTION_TYPE"].eq(option_type)
        )
        if expiry:
            mask = mask & sec_list["SEM_EXPIRY_DATE"].str.contains(expiry)

        matches = sec_list[mask]
        if not matches.empty:
            return str(matches.iloc[0]["SEM_SMST_SECURITY_ID"])
        return ""
