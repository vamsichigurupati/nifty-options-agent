import logging
from datetime import datetime, date
from kiteconnect import KiteConnect
from config.instruments import UNDERLYING_MAP

logger = logging.getLogger(__name__)


class KiteBroker:
    def __init__(self, kite: KiteConnect):
        self.kite = kite
        self._instruments_cache = None
        self._cache_date = None

    def _get_nfo_instruments(self) -> list[dict]:
        """Fetch and cache NFO instruments list (once per day)."""
        today = date.today()
        if self._instruments_cache is None or self._cache_date != today:
            self._instruments_cache = self.kite.instruments("NFO")
            self._cache_date = today
            logger.info(f"Cached {len(self._instruments_cache)} NFO instruments")
        return self._instruments_cache

    def get_option_chain(self, underlying: str, expiry: str = None) -> list[dict]:
        """Fetch CE/PE instruments for given underlying and nearest expiry.

        Args:
            underlying: 'NIFTY' or 'BANKNIFTY'
            expiry: 'YYYY-MM-DD' format, or None for nearest weekly expiry

        Returns list of dicts with instrument details.
        """
        instruments = self._get_nfo_instruments()
        prefix = UNDERLYING_MAP[underlying]["nfo_prefix"]

        # Filter for this underlying's options
        options = [i for i in instruments
                   if i["name"] == prefix
                   and i["instrument_type"] in ("CE", "PE")
                   and i["segment"] == "NFO-OPT"]

        if not options:
            return []

        if expiry:
            expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            options = [i for i in options if i["expiry"] == expiry_date]
        else:
            # Get nearest expiry
            expiries = sorted(set(i["expiry"] for i in options))
            future_expiries = [e for e in expiries if e >= date.today()]
            if not future_expiries:
                return []
            nearest = future_expiries[0]
            options = [i for i in options if i["expiry"] == nearest]

        return [{
            "tradingsymbol": i["tradingsymbol"],
            "strike": i["strike"],
            "instrument_type": i["instrument_type"],
            "instrument_token": i["instrument_token"],
            "lot_size": i["lot_size"],
            "expiry": str(i["expiry"]),
        } for i in options]

    def get_lot_size(self, underlying: str) -> int:
        """Get current lot size for an underlying from instruments list."""
        chain = self.get_option_chain(underlying)
        if chain:
            return chain[0]["lot_size"]
        return 75 if underlying == "NIFTY" else 30  # fallback

    def get_ltp(self, instruments: list[str]) -> dict:
        """Get last traded price. instruments: ['NFO:NIFTY2530622500CE', ...]"""
        return self.kite.ltp(instruments)

    def get_quote(self, instruments: list[str]) -> dict:
        """Get full quote with OI, volume, OHLC."""
        return self.kite.quote(instruments)

    def get_spot_price(self, underlying: str) -> float:
        """Get current spot price of underlying index."""
        info = UNDERLYING_MAP[underlying]
        key = f"{info['exchange']}:{info['tradingsymbol']}"
        data = self.kite.ltp([key])
        return data[key]["last_price"]

    def place_order(self, tradingsymbol: str, exchange: str,
                    transaction_type: str, quantity: int,
                    order_type: str, price: float = None,
                    trigger_price: float = None,
                    product: str = "MIS") -> str:
        """Place order and return order_id."""
        params = {
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "order_type": order_type,
            "product": product,
            "variety": self.kite.VARIETY_REGULAR,
        }
        if price is not None:
            params["price"] = price
        if trigger_price is not None:
            params["trigger_price"] = trigger_price

        order_id = self.kite.place_order(**params)
        logger.info(f"Order placed: {order_id} | {transaction_type} {quantity} {tradingsymbol}")
        return str(order_id)

    def place_bracket_order(self, tradingsymbol: str, transaction_type: str,
                            quantity: int, price: float,
                            stoploss: float, target: float) -> str:
        """Place bracket order with built-in SL and target."""
        order_id = self.kite.place_order(
            variety=self.kite.VARIETY_BO,
            tradingsymbol=tradingsymbol,
            exchange=self.kite.EXCHANGE_NFO,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=self.kite.ORDER_TYPE_LIMIT,
            product=self.kite.PRODUCT_MIS,
            price=price,
            stoploss=stoploss,
            squareoff=target,
        )
        logger.info(f"Bracket order placed: {order_id}")
        return str(order_id)

    def modify_order(self, order_id: str, **params) -> str:
        """Modify existing order (e.g., for trailing stop-loss)."""
        return str(self.kite.modify_order(
            variety=self.kite.VARIETY_REGULAR,
            order_id=order_id,
            **params
        ))

    def cancel_order(self, order_id: str) -> str:
        return str(self.kite.cancel_order(
            variety=self.kite.VARIETY_REGULAR,
            order_id=order_id
        ))

    def get_positions(self) -> list[dict]:
        """Get all current positions with P&L."""
        positions = self.kite.positions()
        return positions.get("net", [])

    def get_order_history(self, order_id: str) -> list[dict]:
        return self.kite.order_history(order_id)

    def get_margins(self) -> dict:
        """Get available margin for F&O trading."""
        margins = self.kite.margins()
        return margins.get("equity", {})
