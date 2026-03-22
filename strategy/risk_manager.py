import logging
from datetime import datetime
from strategy.signal_generator import TradeSignal
from broker.kite_client import KiteBroker
from database.db import get_open_trades, get_daily_pnl
from config.settings import (
    MAX_LOSS_PER_TRADE, MAX_LOSS_PER_DAY, MAX_OPEN_POSITIONS,
    MAX_CAPITAL_PERCENT_PER_TRADE, MIN_RISK_REWARD_RATIO,
    MAX_BID_ASK_SPREAD_PERCENT, SCAN_END
)

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, broker: KiteBroker):
        self.broker = broker

    def can_take_trade(self, signal: TradeSignal) -> tuple[bool, str]:
        """Check all conditions before allowing a trade."""
        # 1. Daily loss limit
        daily_pnl = get_daily_pnl()
        if daily_pnl <= -MAX_LOSS_PER_DAY:
            return False, f"Daily loss limit breached: ₹{daily_pnl:.0f}"

        # 2. Max open positions
        open_trades = get_open_trades()
        if len(open_trades) >= MAX_OPEN_POSITIONS:
            return False, f"Max open positions reached: {len(open_trades)}"

        # 3. Sufficient margin
        try:
            margins = self.broker.get_margins()
            available = margins.get("available", {}).get("live_balance", 0)
            required = signal.entry_price * signal.quantity
            if required > available * MAX_CAPITAL_PERCENT_PER_TRADE * 20:
                # rough check — 5% of capital per trade
                return False, f"Insufficient margin: need ₹{required:.0f}, available ₹{available:.0f}"
        except Exception as e:
            logger.warning(f"Margin check failed: {e}")

        # 4. Risk:reward ratio
        if signal.risk_reward_ratio < MIN_RISK_REWARD_RATIO:
            return False, f"R:R too low: {signal.risk_reward_ratio:.2f} < {MIN_RISK_REWARD_RATIO}"

        # 5. Not within 15 min of market close
        now = datetime.now()
        scan_end = datetime.strptime(SCAN_END, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
        if now >= scan_end:
            return False, "Too close to market close"

        # 6. Check that potential loss is within per-trade limit
        potential_loss = abs(signal.entry_price - signal.stop_loss) * signal.quantity
        if potential_loss > MAX_LOSS_PER_TRADE:
            return False, f"Potential loss ₹{potential_loss:.0f} exceeds per-trade limit ₹{MAX_LOSS_PER_TRADE}"

        return True, "All checks passed"

    def calculate_position_size(self, entry: float, stop_loss: float,
                                 lot_size: int) -> int:
        """Calculate number of lots based on max loss per trade.

        Returns quantity (number of units, multiple of lot_size).
        """
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit <= 0:
            return lot_size  # minimum 1 lot

        max_units = MAX_LOSS_PER_TRADE / risk_per_unit
        lots = int(max_units // lot_size)
        lots = max(lots, 1)  # at least 1 lot

        return lots * lot_size

    def get_stop_loss(self, entry: float, option_type: str) -> float:
        """Default SL: 30% of premium for buying, 50% for selling."""
        if option_type in ("BUY",):
            return round(entry * 0.70, 2)
        else:
            return round(entry * 1.50, 2)

    def get_target(self, entry: float, stop_loss: float,
                   rr_ratio: float = 2.0) -> float:
        """Target based on risk:reward ratio."""
        risk = abs(entry - stop_loss)
        return round(entry + risk * rr_ratio, 2)

    def daily_pnl(self) -> float:
        return get_daily_pnl()
