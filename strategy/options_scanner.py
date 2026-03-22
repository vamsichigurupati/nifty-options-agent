import logging
from dataclasses import dataclass, field
from datetime import datetime, date
import numpy as np
from scipy.stats import norm
from broker.kite_client import KiteBroker
from config.instruments import UNDERLYING_MAP
from config.settings import STRIKES_RANGE, MIN_OPTION_PREMIUM, MAX_BID_ASK_SPREAD_PERCENT

logger = logging.getLogger(__name__)


@dataclass
class TradeSetup:
    underlying: str
    tradingsymbol: str
    strike: float
    instrument_type: str  # CE or PE
    instrument_token: int
    lot_size: int
    expiry: str
    ltp: float
    bid: float
    ask: float
    oi: int
    oi_change: int
    volume: int
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    score: float = 0.0
    spot_price: float = 0.0


def black_scholes_greeks(S, K, T, r, sigma, option_type="CE"):
    """Compute Black-Scholes Greeks.

    Args:
        S: spot price
        K: strike price
        T: time to expiry in years
        r: risk-free rate (decimal)
        sigma: implied volatility (decimal)
        option_type: 'CE' or 'PE'
    """
    if T <= 0 or sigma <= 0:
        return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0}

    d1 = (np.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if option_type == "CE":
        delta = float(norm.cdf(d1))
        theta = float(
            (-(S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T))
             - r * K * np.exp(-r * T) * norm.cdf(d2)) / 365
        )
    else:
        delta = float(norm.cdf(d1) - 1)
        theta = float(
            (-(S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T))
             + r * K * np.exp(-r * T) * norm.cdf(-d2)) / 365
        )

    gamma = float(norm.pdf(d1) / (S * sigma * np.sqrt(T)))
    vega = float(S * norm.pdf(d1) * np.sqrt(T) / 100)

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


class OptionsScanner:
    def __init__(self, broker: KiteBroker):
        self.broker = broker

    def _days_to_expiry(self, expiry_str: str) -> float:
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        delta = (expiry_date - date.today()).days
        return max(delta, 0.01)  # avoid zero

    def scan(self, underlying: str = "NIFTY") -> list[TradeSetup]:
        """Scan option chain and return ranked trade setups."""
        spot = self.broker.get_spot_price(underlying)
        chain = self.broker.get_option_chain(underlying)
        if not chain:
            logger.warning(f"Empty option chain for {underlying}")
            return []

        strike_step = UNDERLYING_MAP[underlying]["strike_step"]
        atm_strike = round(spot / strike_step) * strike_step

        # Filter to ATM ± STRIKES_RANGE
        min_strike = atm_strike - STRIKES_RANGE * strike_step
        max_strike = atm_strike + STRIKES_RANGE * strike_step
        filtered = [o for o in chain if min_strike <= o["strike"] <= max_strike]

        if not filtered:
            return []

        # Fetch quotes for all filtered options
        nfo_keys = [f"NFO:{o['tradingsymbol']}" for o in filtered]
        # Kite quote API allows max ~500 instruments per call
        quotes = {}
        for i in range(0, len(nfo_keys), 200):
            batch = nfo_keys[i:i+200]
            quotes.update(self.broker.get_quote(batch))

        setups = []
        for opt in filtered:
            key = f"NFO:{opt['tradingsymbol']}"
            q = quotes.get(key)
            if not q:
                continue

            ltp = q.get("last_price", 0)
            if ltp < MIN_OPTION_PREMIUM:
                continue

            depth = q.get("depth", {})
            best_bid = depth.get("buy", [{}])[0].get("price", 0) if depth.get("buy") else 0
            best_ask = depth.get("sell", [{}])[0].get("price", 0) if depth.get("sell") else 0

            # Skip wide spreads
            if best_bid > 0 and best_ask > 0:
                spread_pct = ((best_ask - best_bid) / ltp) * 100
                if spread_pct > MAX_BID_ASK_SPREAD_PERCENT:
                    continue

            oi = q.get("oi", 0)
            volume = q.get("volume", 0)
            iv = q.get("ohlc", {}).get("iv", 0)  # Exchange IV if available

            # If IV not in quote, try to use a default
            if not iv:
                iv = 0.20  # 20% default, will be overridden if available

            days = self._days_to_expiry(opt["expiry"])
            T = days / 365.0
            greeks = black_scholes_greeks(
                S=spot, K=opt["strike"], T=T, r=0.065,
                sigma=iv if iv < 1 else iv / 100,  # handle percentage vs decimal
                option_type=opt["instrument_type"]
            )

            setup = TradeSetup(
                underlying=underlying,
                tradingsymbol=opt["tradingsymbol"],
                strike=opt["strike"],
                instrument_type=opt["instrument_type"],
                instrument_token=opt["instrument_token"],
                lot_size=opt["lot_size"],
                expiry=opt["expiry"],
                ltp=ltp,
                bid=best_bid,
                ask=best_ask,
                oi=oi,
                oi_change=q.get("oi_day_high", 0) - q.get("oi_day_low", 0),
                volume=volume,
                iv=iv,
                spot_price=spot,
                **greeks,
            )
            setup.score = self.score_setup(setup)
            setups.append(setup)

        setups.sort(key=lambda s: s.score, reverse=True)
        return setups[:5]

    def score_setup(self, setup: TradeSetup) -> float:
        """Score a trade setup from 0-100."""
        score = 0.0

        # OI score (higher OI = more liquid, better levels) — max 25
        if setup.oi > 1_000_000:
            score += 25
        elif setup.oi > 500_000:
            score += 20
        elif setup.oi > 100_000:
            score += 15
        elif setup.oi > 50_000:
            score += 10

        # Volume score — max 20
        if setup.volume > 100_000:
            score += 20
        elif setup.volume > 50_000:
            score += 15
        elif setup.volume > 10_000:
            score += 10
        elif setup.volume > 5_000:
            score += 5

        # Bid-ask tightness — max 15
        if setup.bid > 0 and setup.ask > 0:
            spread_pct = ((setup.ask - setup.bid) / setup.ltp) * 100
            if spread_pct < 0.5:
                score += 15
            elif spread_pct < 1.0:
                score += 10
            elif spread_pct < 1.5:
                score += 5

        # IV score (moderate IV preferred for buying) — max 15
        iv_pct = setup.iv * 100 if setup.iv < 1 else setup.iv
        if 15 <= iv_pct <= 35:
            score += 15
        elif 10 <= iv_pct <= 50:
            score += 10
        elif iv_pct > 50:
            score += 5  # high IV may be good for selling

        # Delta score (prefer options with delta 0.3-0.5 for buying) — max 15
        abs_delta = abs(setup.delta)
        if 0.3 <= abs_delta <= 0.5:
            score += 15
        elif 0.2 <= abs_delta <= 0.6:
            score += 10
        elif 0.1 <= abs_delta <= 0.7:
            score += 5

        # Premium level (not too cheap, not too expensive) — max 10
        if 100 <= setup.ltp <= 500:
            score += 10
        elif 50 <= setup.ltp <= 1000:
            score += 5

        return score
