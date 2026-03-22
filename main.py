import logging
import threading
import time
import signal
import sys
from datetime import datetime

from config.settings import (
    SCAN_UNDERLYINGS, USE_AI_ANALYSIS, KITE_API_KEY,
    TRADE_CONFIRMATION_TIMEOUT_SECONDS, MAX_LOSS_PER_DAY,
    MAX_LOSS_PER_TRADE
)
from database.db import (
    init_db, insert_signal, update_signal_status, get_pending_signal,
    insert_trade, get_open_trades, get_todays_trades, get_daily_pnl,
    save_daily_summary
)
from broker.kite_auth import KiteAuth
from broker.kite_client import KiteBroker
from broker.kite_websocket import LiveDataStream
from strategy.options_scanner import OptionsScanner
from strategy.signal_generator import RuleBasedSignalGenerator, TradeSignal
from strategy.risk_manager import RiskManager
from strategy.exit_manager import ExitManager, ActiveTrade
from whatsapp.twilio_client import WhatsAppNotifier
from whatsapp.webhook_server import set_trade_handler, start_webhook_server
from ai.trade_analyzer import TradeAnalyzer
from scheduler import TradingScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class TradingAgent:
    def __init__(self):
        self.auth = KiteAuth()
        self.broker = None
        self.ws = None
        self.scanner = None
        self.signal_gen = RuleBasedSignalGenerator()
        self.risk_mgr = None
        self.exit_mgr = None
        self.notifier = WhatsAppNotifier()
        self.ai_analyzer = TradeAnalyzer() if USE_AI_ANALYSIS else None
        self.scheduler = TradingScheduler()

        self._pending_signal = None
        self._pending_signal_id = None
        self._pending_timer = None
        self._signal_lock = threading.Lock()
        self._trading_stopped = False

    # ─── Lifecycle ─────────────────────────────────────────────

    def start(self):
        """Start the trading agent."""
        logger.info("Starting trading agent...")
        init_db()

        # Start webhook server for WhatsApp replies
        set_trade_handler(self._handle_whatsapp_command)
        start_webhook_server()

        # Login and initialize
        self._pre_market_setup()

        # Schedule jobs
        self.scheduler.schedule_pre_market(self._pre_market_setup)
        self.scheduler.schedule_scanner(self._scan_loop)
        self.scheduler.schedule_eod(self._end_of_day)
        self.scheduler.schedule_daily_summary(self._send_daily_summary)
        self.scheduler.schedule_exit_check(self._check_exits)
        self.scheduler.start()

        self.notifier.send_system_status("online")
        logger.info("Trading agent is live")

        # Keep main thread alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        """Gracefully shut down."""
        logger.info("Shutting down...")
        self._trading_stopped = True
        if self.exit_mgr:
            self.exit_mgr.exit_all("SYSTEM_SHUTDOWN")
        if self.ws:
            self.ws.stop()
        self.scheduler.stop()
        self.notifier.send_system_status("offline")
        logger.info("Shutdown complete")

    # ─── Pre-Market Setup ──────────────────────────────────────

    def _pre_market_setup(self):
        """Login to Kite and initialize all components."""
        try:
            kite = self.auth.get_kite()
            self.broker = KiteBroker(kite)
            self.scanner = OptionsScanner(self.broker)
            self.risk_mgr = RiskManager(self.broker)
            self.exit_mgr = ExitManager(self.broker, self.notifier)
            self._trading_stopped = False

            # Start WebSocket
            access_token = self.auth.get_valid_token()
            self.ws = LiveDataStream(KITE_API_KEY, access_token)
            self.ws.on_tick(self._on_tick)
            self.ws.start()

            # Subscribe to spot indices
            from config.instruments import UNDERLYING_MAP
            spot_tokens = [v["spot_token"] for v in UNDERLYING_MAP.values()]
            self.ws.subscribe(spot_tokens)

            logger.info("Pre-market setup complete")

        except Exception as e:
            logger.error(f"Pre-market setup failed: {e}")
            self.notifier.send_error_alert(f"Pre-market setup failed: {e}")

    # ─── Scanning Loop ─────────────────────────────────────────

    def _scan_loop(self):
        """Scan option chains and generate signals."""
        if self._trading_stopped:
            return
        if not self.scheduler.is_market_hours():
            return
        if self._pending_signal is not None:
            return  # Already waiting for confirmation

        # Check daily loss limit
        if get_daily_pnl() <= -MAX_LOSS_PER_DAY:
            if not self._trading_stopped:
                self._trading_stopped = True
                self.notifier.send_error_alert(
                    f"Daily loss limit hit (₹{MAX_LOSS_PER_DAY}). Trading stopped."
                )
            return

        for underlying in SCAN_UNDERLYINGS:
            try:
                signal = self._scan_underlying(underlying)
                if signal:
                    self._send_signal_alert(signal)
                    return  # One signal at a time
            except Exception as e:
                logger.error(f"Scan error for {underlying}: {e}")

    def _scan_underlying(self, underlying: str) -> TradeSignal | None:
        """Scan a single underlying and return a signal if found."""
        setups = self.scanner.scan(underlying)
        if not setups:
            return None

        spot_price = self.broker.get_spot_price(underlying)

        # Try AI analysis first if enabled
        if USE_AI_ANALYSIS and self.ai_analyzer:
            context = {
                "underlying": underlying,
                "spot_price": spot_price,
                "days_to_expiry": self.scanner._days_to_expiry(setups[0].expiry),
                "top_options": [
                    {
                        "tradingsymbol": s.tradingsymbol,
                        "ltp": s.ltp, "oi": s.oi, "volume": s.volume,
                        "iv": s.iv, "delta": s.delta,
                        "bid": s.bid, "ask": s.ask,
                    } for s in setups[:10]
                ],
                "max_risk": MAX_LOSS_PER_TRADE,
                "min_rr": 1.5,
            }
            ai_result = self.ai_analyzer.analyze_market(context)
            if ai_result and ai_result.get("action") not in (None, "NO_TRADE"):
                # Convert AI result to TradeSignal
                lot_size = self.broker.get_lot_size(underlying)
                qty = self.risk_mgr.calculate_position_size(
                    ai_result["entry_price"], ai_result["stop_loss"], lot_size
                )
                signal = TradeSignal(
                    underlying=underlying,
                    tradingsymbol=ai_result["instrument"],
                    action=ai_result["action"],
                    entry_price=ai_result["entry_price"],
                    stop_loss=ai_result["stop_loss"],
                    target=ai_result["target"],
                    quantity=qty,
                    lot_size=lot_size,
                    reasoning=ai_result.get("reasoning", "AI recommendation"),
                    confidence=ai_result.get("confidence", 0.5),
                    risk_reward_ratio=ai_result.get("risk_reward_ratio", 1.5),
                    instrument_type="CE" if "CE" in ai_result["instrument"] else "PE",
                    expiry=setups[0].expiry,
                    spot_price=spot_price,
                )
                allowed, reason = self.risk_mgr.can_take_trade(signal)
                if allowed:
                    return signal
                logger.info(f"AI signal rejected by risk manager: {reason}")

        # Fallback to rule-based
        signal = self.signal_gen.generate(setups, spot_price)
        if signal:
            # Adjust quantity via risk manager
            signal.quantity = self.risk_mgr.calculate_position_size(
                signal.entry_price, signal.stop_loss, signal.lot_size
            )
            allowed, reason = self.risk_mgr.can_take_trade(signal)
            if allowed:
                return signal
            logger.info(f"Rule signal rejected by risk manager: {reason}")

        return None

    # ─── Signal Alert & Confirmation ───────────────────────────

    def _send_signal_alert(self, signal: TradeSignal):
        """Store signal, send WhatsApp alert, start timeout timer."""
        signal_id = insert_signal(
            underlying=signal.underlying,
            tradingsymbol=signal.tradingsymbol,
            action=signal.action,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            target=signal.target,
            quantity=signal.quantity,
            reasoning=signal.reasoning,
            confidence=signal.confidence,
        )

        signal_dict = {
            "tradingsymbol": signal.tradingsymbol,
            "action": signal.action,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "target": signal.target,
            "quantity": signal.quantity,
            "reasoning": signal.reasoning,
            "expiry": signal.expiry,
        }

        with self._signal_lock:
            self._pending_signal = signal
            self._pending_signal_id = signal_id

        self.notifier.send_trade_alert(signal_dict)

        # Start timeout timer
        self._pending_timer = threading.Timer(
            TRADE_CONFIRMATION_TIMEOUT_SECONDS, self._expire_signal
        )
        self._pending_timer.start()
        logger.info(f"Signal {signal_id} sent to WhatsApp, waiting for confirmation")

    def _expire_signal(self):
        """Auto-cancel signal after timeout."""
        with self._signal_lock:
            if self._pending_signal_id:
                update_signal_status(self._pending_signal_id, "EXPIRED")
                logger.info(f"Signal {self._pending_signal_id} expired")
                self._pending_signal = None
                self._pending_signal_id = None

    # ─── WhatsApp Command Handler ──────────────────────────────

    def _handle_whatsapp_command(self, command: str) -> str:
        """Process incoming WhatsApp commands."""
        command = command.strip().upper()

        if command in ("YES", "Y"):
            return self._confirm_trade()
        elif command in ("NO", "N"):
            return self._reject_trade()
        elif command == "STATUS":
            return self._get_status()
        elif command == "POSITIONS":
            return self._get_positions_text()
        elif command == "STOP":
            return self._stop_trading()
        elif command.startswith("EXIT ALL"):
            return self._exit_all()
        elif command.startswith("EXIT "):
            symbol = command[5:].strip()
            return self._exit_position(symbol)
        else:
            return (
                "Commands: YES/NO (trade), STATUS, POSITIONS, "
                "STOP, EXIT ALL, EXIT <symbol>"
            )

    def _confirm_trade(self) -> str:
        with self._signal_lock:
            signal = self._pending_signal
            signal_id = self._pending_signal_id

        if not signal:
            return "No pending trade to confirm."

        if self._pending_timer:
            self._pending_timer.cancel()

        try:
            update_signal_status(signal_id, "CONFIRMED")

            # Place order
            order_id = self.broker.place_order(
                tradingsymbol=signal.tradingsymbol,
                exchange="NFO",
                transaction_type=signal.action,
                quantity=signal.quantity,
                order_type="MARKET",
                product="MIS",
            )

            update_signal_status(signal_id, "EXECUTED")

            # Record trade in DB
            trade_id = insert_trade(
                signal_id=signal_id,
                entry_order_id=order_id,
                tradingsymbol=signal.tradingsymbol,
                action=signal.action,
                entry_price=signal.entry_price,
                quantity=signal.quantity,
                stop_loss=signal.stop_loss,
                target=signal.target,
            )

            # Start monitoring
            active = ActiveTrade(
                trade_id=trade_id,
                tradingsymbol=signal.tradingsymbol,
                action=signal.action,
                entry_price=signal.entry_price,
                quantity=signal.quantity,
                stop_loss=signal.stop_loss,
                target=signal.target,
                entry_time=datetime.now().isoformat(),
                expiry=signal.expiry,
            )
            self.exit_mgr.add_trade(active)

            # Subscribe to option on WebSocket
            chain = self.broker.get_option_chain(signal.underlying)
            for opt in chain:
                if opt["tradingsymbol"] == signal.tradingsymbol:
                    self.ws.subscribe([opt["instrument_token"]])
                    break

            # Send confirmation
            self.notifier.send_execution_confirmation(order_id, {
                "tradingsymbol": signal.tradingsymbol,
                "action": signal.action,
                "price": signal.entry_price,
                "quantity": signal.quantity,
                "stop_loss": signal.stop_loss,
            })

            with self._signal_lock:
                self._pending_signal = None
                self._pending_signal_id = None

            return f"Order executed! ID: {order_id}"

        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            self.notifier.send_error_alert(f"Order failed: {e}")
            return f"Order failed: {e}"

    def _reject_trade(self) -> str:
        with self._signal_lock:
            signal_id = self._pending_signal_id
            self._pending_signal = None
            self._pending_signal_id = None

        if self._pending_timer:
            self._pending_timer.cancel()

        if signal_id:
            update_signal_status(signal_id, "REJECTED")
            return "Trade skipped."
        return "No pending trade to skip."

    def _get_status(self) -> str:
        daily_pnl = get_daily_pnl()
        open_trades = get_open_trades()
        todays = get_todays_trades()
        return (
            f"Status: {'ACTIVE' if not self._trading_stopped else 'STOPPED'}\n"
            f"Open positions: {len(open_trades)}\n"
            f"Today's trades: {len(todays)}\n"
            f"Daily P&L: ₹{daily_pnl:,.0f}"
        )

    def _get_positions_text(self) -> str:
        trades = get_open_trades()
        if not trades:
            return "No open positions."
        lines = []
        for t in trades:
            lines.append(
                f"{t['tradingsymbol']}: {t['action']} @ ₹{t['entry_price']:.2f} | "
                f"SL: ₹{t['stop_loss']:.2f} | TGT: ₹{t['target']:.2f}"
            )
        return "\n".join(lines)

    def _stop_trading(self) -> str:
        self._trading_stopped = True
        return "Trading stopped for today. Send STATUS to check."

    def _exit_all(self) -> str:
        if self.exit_mgr:
            self.exit_mgr.exit_all("MANUAL")
            return "Exiting all positions..."
        return "No active positions."

    def _exit_position(self, symbol: str) -> str:
        trades = get_open_trades()
        for t in trades:
            if symbol in t["tradingsymbol"].upper():
                if self.exit_mgr:
                    active = self.exit_mgr._active_trades.get(t["id"])
                    if active:
                        self.exit_mgr._execute_exit(active, "MANUAL")
                        return f"Exiting {t['tradingsymbol']}..."
        return f"No open position found for {symbol}"

    # ─── Tick Handler ──────────────────────────────────────────

    def _on_tick(self, ticks):
        """Handle incoming WebSocket ticks."""
        if self.exit_mgr:
            for tick in ticks:
                symbol = tick.get("instrument_token")
                price = tick.get("last_price")
                if symbol and price:
                    # Map token back to tradingsymbol (simplified)
                    for trade in list(self.exit_mgr._active_trades.values()):
                        self.exit_mgr.update_price(trade.tradingsymbol, price)

    def _check_exits(self):
        """Periodic exit condition check."""
        if self.exit_mgr:
            self.exit_mgr.check_all_exits()

    # ─── End of Day ────────────────────────────────────────────

    def _end_of_day(self):
        """Square off all positions and wrap up."""
        logger.info("End of day — squaring off positions")
        if self.exit_mgr:
            self.exit_mgr.exit_all("EOD_SQUAREOFF")

    def _send_daily_summary(self):
        """Send daily P&L summary on WhatsApp."""
        trades = get_todays_trades()
        self.notifier.send_daily_summary(trades)

        # Save to DB
        closed = [t for t in trades if t["status"] == "CLOSED"]
        if closed:
            winners = len([t for t in closed if (t.get("net_pnl") or 0) > 0])
            losers = len(closed) - winners
            gross = sum(t.get("pnl") or 0 for t in closed)
            net = sum(t.get("net_pnl") or 0 for t in closed)
            capital = sum(t["entry_price"] * t["quantity"] for t in closed)
            save_daily_summary(len(closed), winners, losers, gross, net, 0, capital)

        self.notifier.send_system_status("offline")
        self._trading_stopped = True
        logger.info("Daily summary sent, system offline")


def main():
    agent = TradingAgent()

    def shutdown(signum, frame):
        agent.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    agent.start()


if __name__ == "__main__":
    main()
