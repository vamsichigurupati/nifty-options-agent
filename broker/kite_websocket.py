import logging
import threading
from kiteconnect import KiteTicker
from config.settings import KITE_API_KEY

logger = logging.getLogger(__name__)


class LiveDataStream:
    def __init__(self, api_key: str, access_token: str):
        self.kws = KiteTicker(api_key, access_token)
        self._subscribed_tokens = []
        self._tick_callback = None
        self._thread = None
        self._connected = False

        self.kws.on_connect = self._on_connect
        self.kws.on_close = self._on_close
        self.kws.on_error = self._on_error
        self.kws.on_reconnect = self._on_reconnect

    def _on_connect(self, ws, response):
        logger.info("WebSocket connected")
        self._connected = True
        if self._subscribed_tokens:
            self.kws.subscribe(self._subscribed_tokens)
            self.kws.set_mode(self.kws.MODE_FULL, self._subscribed_tokens)

    def _on_close(self, ws, code, reason):
        logger.warning(f"WebSocket closed: {code} - {reason}")
        self._connected = False

    def _on_error(self, ws, code, reason):
        logger.error(f"WebSocket error: {code} - {reason}")

    def _on_reconnect(self, ws, attempts_count):
        logger.info(f"WebSocket reconnecting... attempt {attempts_count}")
        if self._subscribed_tokens:
            self.kws.subscribe(self._subscribed_tokens)
            self.kws.set_mode(self.kws.MODE_FULL, self._subscribed_tokens)

    def subscribe(self, instrument_tokens: list[int], mode: str = "full"):
        """Subscribe to instruments. Modes: 'ltp', 'quote', 'full'."""
        self._subscribed_tokens = list(set(self._subscribed_tokens + instrument_tokens))
        mode_map = {
            "ltp": self.kws.MODE_LTP,
            "quote": self.kws.MODE_QUOTE,
            "full": self.kws.MODE_FULL,
        }
        ws_mode = mode_map.get(mode, self.kws.MODE_FULL)

        if self._connected:
            self.kws.subscribe(instrument_tokens)
            self.kws.set_mode(ws_mode, instrument_tokens)

    def unsubscribe(self, instrument_tokens: list[int]):
        self._subscribed_tokens = [t for t in self._subscribed_tokens
                                    if t not in instrument_tokens]
        if self._connected:
            self.kws.unsubscribe(instrument_tokens)

    def on_tick(self, callback):
        """Register tick callback: callback(ticks: list[dict])."""
        self._tick_callback = callback
        self.kws.on_ticks = self._handle_ticks

    def _handle_ticks(self, ws, ticks):
        if self._tick_callback:
            try:
                self._tick_callback(ticks)
            except Exception as e:
                logger.error(f"Tick callback error: {e}")

    def start(self):
        """Start WebSocket in background thread."""
        self._thread = threading.Thread(
            target=self.kws.connect, kwargs={"threaded": True}, daemon=True
        )
        self._thread.start()
        logger.info("WebSocket stream started")

    def stop(self):
        if self._connected:
            self.kws.close()
            self._connected = False
            logger.info("WebSocket stream stopped")

    @property
    def is_connected(self) -> bool:
        return self._connected
