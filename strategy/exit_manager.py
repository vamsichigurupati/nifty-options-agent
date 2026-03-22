import logging
import threading
from datetime import datetime, date
from broker.kite_client import KiteBroker
from database.db import close_trade, get_trade
from config.settings import (
    TRAILING_SL_TRIGGER_PERCENT, TRAILING_SL_BREAKEVEN_PERCENT,
    TRAILING_SL_PROFIT_LOCK_PERCENT, MAX_HOLDING_MINUTES,
    EOD_EXIT_TIME, EXPIRY_DAY_EXIT_TIME,
    BROKERAGE_PER_ORDER, STT_PERCENT_SELL, GST_PERCENT,
    STAMP_DUTY_PERCENT, SEBI_CHARGES_PERCENT, EXCHANGE_TXN_PERCENT
)

logger = logging.getLogger(__name__)


class ActiveTrade:
    def __init__(self, trade_id: int, tradingsymbol: str, action: str,
                 entry_price: float, quantity: int, stop_loss: float,
                 target: float, entry_time: str, expiry: str = None,
                 entry_iv: float = None):
        self.trade_id = trade_id
        self.tradingsymbol = tradingsymbol
        self.action = action
        self.entry_price = entry_price
        self.quantity = quantity
        self.stop_loss = stop_loss
        self.original_sl = stop_loss
        self.target = target
        self.entry_time = datetime.fromisoformat(entry_time)
        self.expiry = expiry
        self.entry_iv = entry_iv
        self.current_price = entry_price
        self.trailing_active = False


class ExitManager:
    def __init__(self, broker: KiteBroker, whatsapp_notifier=None):
        self.broker = broker
        self.notifier = whatsapp_notifier
        self._active_trades: dict[int, ActiveTrade] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def add_trade(self, trade: ActiveTrade):
        with self._lock:
            self._active_trades[trade.trade_id] = trade
        logger.info(f"Monitoring trade {trade.trade_id}: {trade.tradingsymbol}")

    def remove_trade(self, trade_id: int):
        with self._lock:
            self._active_trades.pop(trade_id, None)

    def update_price(self, tradingsymbol: str, price: float):
        """Called on each tick to update price and check exits."""
        with self._lock:
            for trade in self._active_trades.values():
                if trade.tradingsymbol == tradingsymbol:
                    trade.current_price = price
                    self._check_exit(trade)

    def check_all_exits(self):
        """Periodic check for time-based exits."""
        with self._lock:
            for trade in list(self._active_trades.values()):
                self._check_exit(trade)

    def _check_exit(self, trade: ActiveTrade):
        """Check all exit conditions for a trade."""
        price = trade.current_price

        # 1. Stop loss hit
        if trade.action == "BUY" and price <= trade.stop_loss:
            self._execute_exit(trade, "SL_HIT")
            return

        # For SELL trades, SL is above entry
        if trade.action == "SELL" and price >= trade.stop_loss:
            self._execute_exit(trade, "SL_HIT")
            return

        # 2. Target hit
        if trade.action == "BUY" and price >= trade.target:
            self._execute_exit(trade, "TARGET_HIT")
            return

        if trade.action == "SELL" and price <= trade.target:
            self._execute_exit(trade, "TARGET_HIT")
            return

        # 3. Trailing stop loss
        if trade.action == "BUY":
            self._update_trailing_sl(trade)

        # 4. Time-based exit
        now = datetime.now()
        is_expiry = trade.expiry and trade.expiry == str(date.today())
        exit_time_str = EXPIRY_DAY_EXIT_TIME if is_expiry else EOD_EXIT_TIME
        exit_time = datetime.strptime(exit_time_str, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
        if now >= exit_time:
            self._execute_exit(trade, "TIME_EXIT" if is_expiry else "EOD_SQUAREOFF")
            return

        # 5. Max holding time with low profit
        holding_mins = (now - trade.entry_time).total_seconds() / 60
        if holding_mins > MAX_HOLDING_MINUTES:
            profit_pct = ((price - trade.entry_price) / trade.entry_price) * 100
            if profit_pct < 10:
                self._execute_exit(trade, "MAX_HOLD_TIME")
                return

    def _update_trailing_sl(self, trade: ActiveTrade):
        """Update trailing stop loss based on price movement."""
        price = trade.current_price
        entry = trade.entry_price
        target = trade.target

        total_move = target - entry
        if total_move <= 0:
            return

        current_move = price - entry
        move_pct = (current_move / total_move) * 100

        # Trail SL to breakeven when price reaches 50% of target move
        if move_pct >= TRAILING_SL_TRIGGER_PERCENT:
            new_sl = entry  # breakeven
            if new_sl > trade.stop_loss:
                trade.stop_loss = new_sl
                trade.trailing_active = True
                logger.info(f"Trade {trade.trade_id}: SL trailed to breakeven ₹{new_sl:.2f}")

        # Lock 50% of profit when price reaches 75% of target
        if move_pct >= TRAILING_SL_PROFIT_LOCK_PERCENT:
            new_sl = entry + current_move * 0.50
            if new_sl > trade.stop_loss:
                trade.stop_loss = round(new_sl, 2)
                logger.info(f"Trade {trade.trade_id}: SL trailed to ₹{trade.stop_loss:.2f} (50% profit locked)")

    def _execute_exit(self, trade: ActiveTrade, reason: str):
        """Place exit order, update DB, notify."""
        try:
            # Place opposite order
            exit_action = "SELL" if trade.action == "BUY" else "BUY"
            exit_order_id = self.broker.place_order(
                tradingsymbol=trade.tradingsymbol,
                exchange="NFO",
                transaction_type=exit_action,
                quantity=trade.quantity,
                order_type="MARKET",
                product="MIS",
            )

            # Calculate P&L
            if trade.action == "BUY":
                pnl = (trade.current_price - trade.entry_price) * trade.quantity
            else:
                pnl = (trade.entry_price - trade.current_price) * trade.quantity

            charges = self._calculate_charges(trade.entry_price, trade.current_price,
                                               trade.quantity)

            close_trade(
                trade_id=trade.trade_id,
                exit_order_id=exit_order_id,
                exit_price=trade.current_price,
                exit_reason=reason,
                pnl=pnl,
                charges=charges,
            )

            logger.info(
                f"Trade {trade.trade_id} exited: {reason} | "
                f"P&L: ₹{pnl:.2f} | Net: ₹{pnl - charges:.2f}"
            )

            # Send WhatsApp notification
            if self.notifier:
                trade_data = get_trade(trade.trade_id)
                if trade_data:
                    self.notifier.send_exit_notification(trade_data)

        except Exception as e:
            logger.error(f"Exit failed for trade {trade.trade_id}: {e}")
        finally:
            self.remove_trade(trade.trade_id)

    def _calculate_charges(self, entry_price: float, exit_price: float,
                            quantity: int) -> float:
        """Calculate approximate trading charges."""
        turnover = (entry_price + exit_price) * quantity
        brokerage = min(BROKERAGE_PER_ORDER * 2, turnover * 0.0003)  # max 0.03%
        stt = exit_price * quantity * STT_PERCENT_SELL / 100
        gst = brokerage * GST_PERCENT
        stamp = entry_price * quantity * STAMP_DUTY_PERCENT / 100
        sebi = turnover * SEBI_CHARGES_PERCENT / 100
        exchange = turnover * EXCHANGE_TXN_PERCENT / 100

        return round(brokerage + stt + gst + stamp + sebi + exchange, 2)

    def exit_all(self, reason: str = "MANUAL"):
        """Exit all active positions immediately."""
        with self._lock:
            for trade in list(self._active_trades.values()):
                self._execute_exit(trade, reason)
