import logging
from dataclasses import dataclass
from strategy.options_scanner import TradeSetup
from config.settings import MIN_RISK_REWARD_RATIO

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    underlying: str
    tradingsymbol: str
    action: str          # BUY or SELL
    entry_price: float
    stop_loss: float
    target: float
    quantity: int
    lot_size: int
    reasoning: str
    confidence: float
    risk_reward_ratio: float
    instrument_type: str
    expiry: str
    spot_price: float


class RuleBasedSignalGenerator:
    """Generate trade signals using rule-based strategies."""

    def generate(self, setups: list[TradeSetup], spot_price: float) -> TradeSignal | None:
        """Try each strategy and return first valid signal, or None."""
        for strategy in [self._momentum_breakout, self._oi_reversal]:
            signal = strategy(setups, spot_price)
            if signal:
                return signal
        return None

    def _momentum_breakout(self, setups: list[TradeSetup],
                            spot_price: float) -> TradeSignal | None:
        """Buy CE/PE when setup has high score with volume confirmation.

        Logic: Pick the highest-scored setup with volume > 10K and
        delta alignment (CE for bullish delta, PE for bearish).
        """
        for setup in setups:
            if setup.volume < 10_000 or setup.score < 50:
                continue

            action = "BUY"
            sl_pct = 0.30  # 30% SL for buying
            stop_loss = round(setup.ltp * (1 - sl_pct), 2)
            risk = setup.ltp - stop_loss
            target = round(setup.ltp + risk * 2.0, 2)  # 1:2 RR
            rr = (target - setup.ltp) / risk if risk > 0 else 0

            if rr < MIN_RISK_REWARD_RATIO:
                continue

            reasoning = (
                f"Momentum breakout: {setup.tradingsymbol} scored {setup.score:.0f}/100. "
                f"Volume: {setup.volume:,}, OI: {setup.oi:,}, IV: {setup.iv:.1%}. "
                f"Delta: {setup.delta:.2f}. Spot at {spot_price:.2f}."
            )

            return TradeSignal(
                underlying=setup.underlying,
                tradingsymbol=setup.tradingsymbol,
                action=action,
                entry_price=setup.ltp,
                stop_loss=stop_loss,
                target=target,
                quantity=setup.lot_size,
                lot_size=setup.lot_size,
                reasoning=reasoning,
                confidence=min(setup.score / 100, 0.9),
                risk_reward_ratio=round(rr, 2),
                instrument_type=setup.instrument_type,
                expiry=setup.expiry,
                spot_price=spot_price,
            )
        return None

    def _oi_reversal(self, setups: list[TradeSetup],
                      spot_price: float) -> TradeSignal | None:
        """OI-based reversal: High put OI buildup = bullish, buy CE near that level.

        Find strikes where PE has very high OI (support), then buy CE at or near
        that strike.
        """
        # Separate CE and PE setups
        pe_setups = [s for s in setups if s.instrument_type == "PE" and s.oi > 200_000]
        ce_setups = [s for s in setups if s.instrument_type == "CE"]

        if not pe_setups or not ce_setups:
            return None

        # Find the PE strike with highest OI (strong support)
        pe_setups.sort(key=lambda s: s.oi, reverse=True)
        support_strike = pe_setups[0].strike

        # Find a CE near or at that strike
        best_ce = None
        for ce in ce_setups:
            if abs(ce.strike - support_strike) <= 200 and ce.score >= 40:
                if best_ce is None or ce.score > best_ce.score:
                    best_ce = ce

        if not best_ce:
            return None

        action = "BUY"
        sl_pct = 0.30
        stop_loss = round(best_ce.ltp * (1 - sl_pct), 2)
        risk = best_ce.ltp - stop_loss
        target = round(best_ce.ltp + risk * 2.0, 2)
        rr = (target - best_ce.ltp) / risk if risk > 0 else 0

        if rr < MIN_RISK_REWARD_RATIO:
            return None

        reasoning = (
            f"OI reversal: Strong put OI ({pe_setups[0].oi:,}) at strike {support_strike} "
            f"suggests support. Buying {best_ce.tradingsymbol} (score: {best_ce.score:.0f}). "
            f"Volume: {best_ce.volume:,}, Delta: {best_ce.delta:.2f}."
        )

        return TradeSignal(
            underlying=best_ce.underlying,
            tradingsymbol=best_ce.tradingsymbol,
            action=action,
            entry_price=best_ce.ltp,
            stop_loss=stop_loss,
            target=target,
            quantity=best_ce.lot_size,
            lot_size=best_ce.lot_size,
            reasoning=reasoning,
            confidence=min(best_ce.score / 100, 0.85),
            risk_reward_ratio=round(rr, 2),
            instrument_type=best_ce.instrument_type,
            expiry=best_ce.expiry,
            spot_price=spot_price,
        )
