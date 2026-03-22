SYSTEM_PROMPT = """You are an expert Indian options trader specializing in Nifty 50 and Bank Nifty index options. You analyze market data and provide trade recommendations.

RULES:
1. You MUST respond with valid JSON only — no markdown, no explanation outside JSON.
2. Be conservative — only recommend trades with high confidence (>0.6).
3. Every recommendation MUST include stop_loss and target.
4. Consider time to expiry — avoid buying far OTM options near expiry.
5. Prefer liquid strikes (high OI, tight spreads).
6. Factor in IV percentile — avoid buying when IV is extremely high.
7. Risk:reward must be at least 1:1.5.

RESPONSE FORMAT:
{
    "action": "BUY" | "SELL" | "NO_TRADE",
    "instrument": "<tradingsymbol like NIFTY2530622500CE>",
    "entry_price": <float>,
    "stop_loss": <float>,
    "target": <float>,
    "reasoning": "<clear explanation for WhatsApp alert>",
    "confidence": <float 0-1>,
    "risk_reward_ratio": <float>
}

If no good trade is available, respond with:
{"action": "NO_TRADE", "reasoning": "explanation why no trade"}
"""


def build_analysis_prompt(context: dict) -> str:
    """Build the user prompt with market data context."""
    options_text = ""
    for opt in context.get("top_options", []):
        options_text += (
            f"  {opt['tradingsymbol']}: LTP={opt['ltp']:.2f}, "
            f"OI={opt.get('oi', 0):,}, Vol={opt.get('volume', 0):,}, "
            f"IV={opt.get('iv', 0):.1%}, Delta={opt.get('delta', 0):.2f}, "
            f"Bid={opt.get('bid', 0):.2f}, Ask={opt.get('ask', 0):.2f}\n"
        )

    return f"""Analyze the following market snapshot and recommend a trade (or NO_TRADE):

UNDERLYING: {context.get('underlying', 'NIFTY')}
SPOT PRICE: {context.get('spot_price', 0):.2f}
TIME TO EXPIRY: {context.get('days_to_expiry', 0)} days

TOP OPTIONS BY SCORE:
{options_text}

RISK PARAMETERS:
- Max loss per trade: INR {context.get('max_risk', 2000)}
- Min risk:reward: 1:{context.get('min_rr', 1.5)}

Provide your analysis as JSON."""


def build_exit_prompt(position: dict, market_data: dict) -> str:
    """Build prompt for exit evaluation."""
    return f"""Evaluate whether to HOLD, EXIT, or ADJUST this position:

POSITION:
- Symbol: {position['tradingsymbol']}
- Action: {position['action']}
- Entry: INR {position['entry_price']:.2f}
- Current: INR {market_data.get('current_price', 0):.2f}
- Stop Loss: INR {position['stop_loss']:.2f}
- Target: INR {position['target']:.2f}
- Holding time: {market_data.get('holding_minutes', 0)} minutes
- Current P&L: INR {market_data.get('unrealized_pnl', 0):.2f}

CURRENT MARKET:
- Spot: {market_data.get('spot_price', 0):.2f}
- Option IV: {market_data.get('current_iv', 0):.1%}
- Entry IV: {market_data.get('entry_iv', 0):.1%}

Respond with JSON:
{{"decision": "HOLD" | "EXIT" | "ADJUST", "new_stop_loss": <float or null>, "new_target": <float or null>, "reasoning": "explanation"}}"""
