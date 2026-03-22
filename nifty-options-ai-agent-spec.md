# AI Options Trading Agent — Build Specification

## Project: Nifty/BankNifty Options AI Agent with WhatsApp Alerts & Zerodha Kite Execution

---

## 1. Overview

Build a Python-based AI trading agent that:

1. **Scans** Nifty 50 and Bank Nifty options chains in real-time during market hours (9:15 AM – 3:30 PM IST)
2. **Analyzes** options data (Greeks, IV, OI, price action) using AI to identify high-probability trade setups
3. **Sends trade alerts** to the user's WhatsApp with entry price, stop-loss, and target
4. **Waits for user confirmation** ("YES" / "NO") on WhatsApp before executing
5. **Executes the order** on Zerodha via Kite Connect API upon approval
6. **Monitors the position** and auto-manages exits (trailing stop-loss, target hit, time-based exit)
7. **Sends exit notifications** back on WhatsApp with P&L summary

---

## 2. Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11+ |
| Broker API | Zerodha Kite Connect (`kiteconnect` PyPI package) |
| WhatsApp | Twilio WhatsApp Business API |
| AI/LLM | Claude API (Anthropic) for trade reasoning, OR rule-based strategy engine |
| Web Framework | Flask (for Twilio webhook) |
| Task Scheduler | APScheduler or Celery |
| Database | SQLite (for trade log, session tokens, state) |
| Hosting | Any VPS with public URL (Railway, Render, AWS EC2, DigitalOcean) |
| Tunnel (dev) | ngrok (for local Twilio webhook testing) |

---

## 3. Project Structure

```
nifty-options-agent/
├── config/
│   ├── settings.py          # All config: API keys, thresholds, risk params
│   └── instruments.py       # Nifty/BankNifty instrument token mappings
├── broker/
│   ├── kite_auth.py         # Kite Connect login + TOTP automation
│   ├── kite_client.py       # Order placement, position fetch, margin check
│   └── kite_websocket.py    # Live tick streaming via KiteTicker
├── strategy/
│   ├── options_scanner.py   # Fetch option chain, compute Greeks, rank setups
│   ├── signal_generator.py  # AI/rule-based signal generation
│   ├── risk_manager.py      # Position sizing, max loss, SL/target calc
│   └── exit_manager.py      # Trailing SL, time-based exit, target exit logic
├── whatsapp/
│   ├── twilio_client.py     # Send WhatsApp messages via Twilio
│   ├── webhook_server.py    # Flask app to receive WhatsApp replies
│   └── message_templates.py # Format trade alert / exit / P&L messages
├── ai/
│   ├── trade_analyzer.py    # Claude API integration for trade reasoning
│   └── prompts.py           # System + user prompts for AI analysis
├── database/
│   ├── models.py            # SQLite models: trades, signals, sessions
│   └── db.py                # DB connection and helpers
├── main.py                  # Entry point: orchestrates the full loop
├── scheduler.py             # APScheduler jobs for market hours
├── requirements.txt
├── .env                     # Secrets (NEVER commit)
└── README.md
```

---

## 4. Environment Variables (`.env`)

```env
# ─── Zerodha Kite Connect ───
KITE_API_KEY=your_kite_api_key
KITE_API_SECRET=your_kite_api_secret
KITE_USER_ID=your_zerodha_client_id
KITE_PASSWORD=your_zerodha_password
KITE_TOTP_SECRET=your_totp_secret_key   # Base32 secret from Zerodha TOTP setup

# ─── Twilio WhatsApp ───
TWILIO_ACCOUNT_SID=your_twilio_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886   # Twilio sandbox number
USER_WHATSAPP_TO=whatsapp:+91XXXXXXXXXX       # Your WhatsApp number

# ─── Claude AI (optional, for AI-driven analysis) ───
ANTHROPIC_API_KEY=your_anthropic_api_key

# ─── App Config ───
FLASK_PORT=5000
DATABASE_PATH=./data/trades.db
WEBHOOK_URL=https://your-domain.com/whatsapp/webhook   # Public URL for Twilio
```

---

## 5. Module Specifications

### 5.1 `broker/kite_auth.py` — Automated Kite Login

Automate the daily Kite Connect login flow. Kite requires a browser-based OAuth login that produces a `request_token`, which is exchanged for an `access_token`.

```
FLOW:
1. Generate TOTP using pyotp and KITE_TOTP_SECRET
2. Open kite.login_url() programmatically
3. POST credentials + TOTP to Zerodha login endpoint
4. Extract request_token from redirect URL
5. Call kite.generate_session(request_token, api_secret)
6. Store access_token in DB with timestamp
7. Token is valid for one trading day (until ~6 AM next day)
```

**Dependencies:** `kiteconnect`, `pyotp`, `requests`

**Key methods:**
- `login() -> str` — returns access_token
- `get_valid_token() -> str` — returns cached token if still valid, else re-login
- `is_token_valid() -> bool` — check if stored token works

**Important:** The TOTP secret is the base32 string you get when setting up the authenticator app on Zerodha (not the 6-digit code). Use `pyotp.TOTP(secret).now()` to generate the current code.

### 5.2 `broker/kite_client.py` — Trading Operations

Wrapper around KiteConnect for all trading operations.

**Key methods:**
```python
class KiteBroker:
    def get_option_chain(self, underlying: str, expiry: str) -> list[dict]
        """Fetch all CE/PE instruments for given underlying and expiry.
        underlying: 'NIFTY' or 'BANKNIFTY'
        Returns list of dicts with: tradingsymbol, strike, instrument_type, 
        instrument_token, lot_size, expiry"""
    
    def get_ltp(self, instruments: list[str]) -> dict
        """Get last traded price for list of instruments.
        instruments: ['NFO:NIFTY2530622500CE', ...]"""
    
    def get_option_greeks(self, instrument_token: int) -> dict
        """Fetch full quote with Greeks (requires MODE_FULL on websocket).
        Returns: ltp, oi, volume, bid, ask, iv (implied volatility)"""
    
    def place_order(self, tradingsymbol: str, exchange: str, 
                    transaction_type: str, quantity: int,
                    order_type: str, price: float = None,
                    trigger_price: float = None,
                    product: str = "MIS") -> str
        """Place order and return order_id.
        product: MIS for intraday, NRML for overnight
        order_type: MARKET, LIMIT, SL, SL-M"""
    
    def place_bracket_order(self, tradingsymbol: str, transaction_type: str,
                           quantity: int, price: float, 
                           stoploss: float, target: float) -> str
        """Place bracket order with built-in SL and target."""
    
    def modify_order(self, order_id: str, **params) -> str
        """Modify existing order (for trailing stop-loss)."""
    
    def cancel_order(self, order_id: str) -> str
    
    def get_positions(self) -> list[dict]
        """Get all current positions with P&L."""
    
    def get_order_history(self, order_id: str) -> list[dict]
    
    def get_margins(self) -> dict
        """Get available margin for F&O trading."""
```

**Exchange codes:** Use `kite.EXCHANGE_NFO` for all F&O orders.

**Lot sizes (as of 2025):**
- NIFTY: 75 units per lot (verify — this changes periodically)
- BANKNIFTY: 30 units per lot (verify — this changes periodically)

**IMPORTANT:** Always check lot sizes dynamically from `kite.instruments("NFO")` as SEBI revises these.

### 5.3 `broker/kite_websocket.py` — Live Data Streaming

Stream real-time ticks for subscribed instruments.

```python
class LiveDataStream:
    def __init__(self, api_key, access_token):
        self.kws = KiteTicker(api_key, access_token)
    
    def subscribe(self, instrument_tokens: list[int], mode: str = "full"):
        """Subscribe to instruments. 
        Modes: 'ltp' (just price), 'quote' (OHLC+vol+OI), 'full' (all + Greeks)"""
    
    def on_tick(self, callback):
        """Register callback: callback(ticks: list[dict])"""
    
    def start(self):
        """Start WebSocket in background thread."""
```

**Tick data (MODE_FULL) includes:** last_price, ohlc, volume, oi, change, 
last_trade_time, bid/ask depth, and for options: iv (implied volatility from exchange).

### 5.4 `strategy/options_scanner.py` — Options Chain Scanner

Scans the option chain and identifies potential trades.

```python
class OptionsScanner:
    def scan(self, underlying: str = "NIFTY") -> list[TradeSetup]:
        """
        1. Fetch current spot price of underlying
        2. Get nearest weekly expiry option chain
        3. Filter strikes: ATM ± 10 strikes (both CE and PE)
        4. For each option, compute/fetch:
           - LTP, bid-ask spread
           - Open Interest (OI) and OI change
           - Implied Volatility (IV)
           - Volume
           - Greeks: Delta, Gamma, Theta, Vega (compute via Black-Scholes 
             if not available from exchange)
        5. Rank options by scoring criteria (see below)
        6. Return top 3-5 setups
        """

    def compute_greeks(self, spot, strike, expiry_days, iv, option_type, risk_free_rate=0.065):
        """Black-Scholes Greeks calculator.
        Returns: delta, gamma, theta, vega"""

    def score_setup(self, option_data: dict) -> float:
        """Score a potential trade setup (0-100) based on:
        - OI buildup (high OI = support/resistance)
        - IV percentile (is IV relatively high or low?)
        - Bid-ask spread tightness
        - Volume (higher = more liquid)
        - Greeks alignment with strategy
        - Price relative to support/resistance levels
        """
```

**Scanning should happen:**
- Every 5 minutes during market hours
- Extra scan at 9:20 AM (post-opening volatility settles)
- Extra scan at 2:30 PM (for expiry-day trades)

### 5.5 `strategy/signal_generator.py` — Trade Signal Generation

Generates actionable trade signals from scanned data. Support both rule-based and AI-powered modes.

**Rule-Based Strategy Options (implement at least 2):**

1. **Momentum Breakout:** Buy CE/PE when underlying breaks above/below a key level with volume confirmation
2. **OI-Based Reversal:** When put OI builds up significantly at a strike (bullish), buy CE near that strike
3. **IV Crush Play:** Sell options when IV is at extreme highs (>75th percentile of 30-day range)
4. **Supertrend Strategy:** Use Supertrend indicator on 5-min chart for directional bias, then buy corresponding option
5. **Straddle/Strangle Selling:** Sell ATM straddle or OTM strangle when IV rank > 50

**AI-Powered Mode (Claude Integration):**

```python
class AISignalGenerator:
    def analyze(self, market_data: dict) -> TradeSignal:
        """
        Send market context to Claude API and get trade recommendation.
        
        Prompt should include:
        - Current spot price, trend (5min, 15min, 1hr)
        - Top 10 options by OI with their Greeks
        - Recent OI change data
        - IV percentile
        - Support/resistance levels
        - Time to expiry
        - Current market sentiment indicators
        
        Claude should respond with structured JSON:
        {
            "action": "BUY" | "SELL" | "NO_TRADE",
            "instrument": "NIFTY2530622500CE",
            "entry_price": 185.50,
            "stop_loss": 150.00,
            "target": 250.00,
            "reasoning": "...",
            "confidence": 0.75,
            "risk_reward_ratio": 1.85
        }
        """
```

### 5.6 `strategy/risk_manager.py` — Risk Management

**CRITICAL MODULE — this prevents catastrophic losses.**

```python
class RiskManager:
    # ─── Configurable Parameters ───
    MAX_LOSS_PER_TRADE: float = 2000      # Max ₹ loss per single trade
    MAX_LOSS_PER_DAY: float = 5000        # Max ₹ loss per day (stop trading after)
    MAX_OPEN_POSITIONS: int = 2           # Max simultaneous positions
    MAX_CAPITAL_PER_TRADE: float = 0.05   # Max 5% of total capital per trade
    MIN_RISK_REWARD: float = 1.5          # Minimum risk:reward ratio
    
    def can_take_trade(self, signal: TradeSignal) -> tuple[bool, str]:
        """Check ALL conditions before allowing a trade:
        1. Daily loss limit not breached
        2. Max open positions not exceeded
        3. Sufficient margin available
        4. Risk:reward ratio >= MIN_RISK_REWARD
        5. Not within 15 min of market close (avoid illiquidity)
        6. Bid-ask spread < 2% of option price (liquidity check)
        Returns (allowed: bool, reason: str)"""
    
    def calculate_position_size(self, entry: float, stop_loss: float, 
                                 capital: float) -> int:
        """Calculate number of lots based on max loss per trade.
        lots = MAX_LOSS_PER_TRADE / (abs(entry - stop_loss) * lot_size)
        Round DOWN to nearest lot."""
    
    def get_stop_loss(self, entry: float, option_type: str) -> float:
        """Default SL: 30% of premium for buying, 50% of premium for selling."""
    
    def get_target(self, entry: float, stop_loss: float, rr_ratio: float = 2.0) -> float:
        """Target based on risk:reward ratio."""
    
    def daily_pnl(self) -> float:
        """Sum of all closed + open trade P&L today."""
```

### 5.7 `strategy/exit_manager.py` — Automated Exit Logic

```python
class ExitManager:
    def monitor_position(self, trade: ActiveTrade):
        """Continuously monitor an active position and exit when conditions met.
        
        Exit conditions (check every tick or every 5 seconds):
        1. STOP LOSS HIT: price <= stop_loss → immediate market exit
        2. TARGET HIT: price >= target → immediate market exit
        3. TRAILING SL: if price moves 50%+ toward target, 
           trail SL to breakeven. If price hits 75%+ of target, 
           trail SL to 50% of profit.
        4. TIME-BASED EXIT: 
           - Exit all MIS positions by 3:15 PM
           - If expiry day, exit by 3:20 PM
        5. IV CRUSH EXIT: If IV drops > 20% from entry IV, consider exit
        6. MAX HOLDING TIME: Exit if trade open > 2 hours with < 10% profit
        
        On exit, send WhatsApp notification with P&L.
        """
    
    def trail_stop_loss(self, trade: ActiveTrade, current_price: float) -> float:
        """Calculate new trailing SL based on current price movement."""
    
    def execute_exit(self, trade: ActiveTrade, reason: str):
        """Place exit order and log the trade.
        1. Place market sell/buy order (opposite of entry)
        2. Update trade record in DB
        3. Send WhatsApp P&L message
        4. Update daily P&L tracker"""
```

### 5.8 `whatsapp/twilio_client.py` — Send WhatsApp Messages

```python
from twilio.rest import Client

class WhatsAppNotifier:
    def __init__(self):
        self.client = Client(TWILIO_SID, TWILIO_TOKEN)
    
    def send_trade_alert(self, signal: TradeSignal):
        """Send formatted trade alert. Example message:
        
        🔔 *TRADE ALERT*
        ━━━━━━━━━━━━━━━
        📊 NIFTY 22500 CE
        📅 Expiry: 06-Mar-2025
        
        ▶️ Action: BUY
        💰 Entry: ₹185.50
        🛑 Stop Loss: ₹150.00 (-19.1%)
        🎯 Target: ₹250.00 (+34.8%)
        📐 Risk:Reward: 1:1.82
        📦 Qty: 75 (1 lot)
        💵 Capital Required: ~₹13,912
        
        📝 Reason: Strong OI buildup at 22400PE 
        suggesting support. Momentum bullish on 
        15-min chart. IV at 45th percentile.
        
        ✅ Reply YES to execute
        ❌ Reply NO to skip
        ⏰ Alert expires in 5 minutes
        """
    
    def send_execution_confirmation(self, order_id: str, details: dict):
        """✅ ORDER EXECUTED
        Order ID: 240306000123
        NIFTY 22500 CE BUY @ ₹186.00
        Qty: 75 | Product: MIS
        SL order placed at ₹150.00"""
    
    def send_exit_notification(self, trade: CompletedTrade):
        """📊 POSITION CLOSED
        NIFTY 22500 CE
        Entry: ₹186.00 → Exit: ₹238.00
        P&L: +₹3,900.00 (+27.9%)
        Exit Reason: Target hit
        Duration: 47 minutes"""
    
    def send_daily_summary(self):
        """📈 DAILY SUMMARY
        Total Trades: 3
        Winners: 2 | Losers: 1
        Gross P&L: +₹5,200
        Net P&L (after charges): +₹4,850
        Capital Used: ₹42,000
        ROI: 11.5%"""
    
    def send_error_alert(self, error: str):
        """⚠️ SYSTEM ERROR: {error}"""
```

### 5.9 `whatsapp/webhook_server.py` — Receive WhatsApp Replies

Flask server to handle incoming WhatsApp messages via Twilio webhook.

```python
from flask import Flask, request

app = Flask(__name__)

@app.route("/whatsapp/webhook", methods=["POST"])
def whatsapp_webhook():
    """Handle incoming WhatsApp messages from Twilio.
    
    Twilio sends POST with form data:
    - Body: message text
    - From: sender's WhatsApp number
    - To: Twilio number
    
    Logic:
    1. Verify the message is from the authorized user number
    2. Parse the message body:
       - "YES" or "Y" → execute the pending trade
       - "NO" or "N" → cancel the pending trade
       - "STATUS" → send current positions and P&L
       - "STOP" → stop all trading for the day
       - "POSITIONS" → list all open positions
       - "EXIT <symbol>" → manually exit a specific position
       - "EXIT ALL" → exit all positions immediately
    3. Respond with confirmation via Twilio
    
    IMPORTANT: Implement a 5-minute timeout for trade confirmations.
    If no reply within 5 minutes, auto-cancel the signal.
    """
```

### 5.10 `ai/trade_analyzer.py` — Claude AI Integration

```python
import anthropic

class TradeAnalyzer:
    def __init__(self):
        self.client = anthropic.Anthropic()
    
    def analyze_market(self, context: dict) -> dict:
        """Send market snapshot to Claude for analysis.
        
        context includes:
        - spot_price, trend_5min, trend_15min, trend_1hr
        - option_chain (top 20 by OI)
        - iv_percentile
        - support_resistance_levels
        - recent_news_sentiment (optional)
        - time_to_expiry
        - max_risk_per_trade
        
        Use claude-sonnet-4-20250514 model for cost efficiency.
        
        System prompt should enforce:
        - JSON-only response
        - Conservative bias (only recommend high-confidence trades)
        - Always include stop-loss and target
        - Explain reasoning clearly (shown to user in WhatsApp)
        """
    
    def evaluate_exit(self, position: dict, market_data: dict) -> dict:
        """Ask Claude whether to hold, exit, or adjust SL/target
        given current market conditions."""
```

---

## 6. Main Orchestration Loop (`main.py`)

```python
"""
MAIN LOOP (runs during market hours 9:15 AM - 3:30 PM IST):

1. PRE-MARKET (9:00 AM):
   - Auto-login to Kite Connect
   - Verify margins and positions
   - Subscribe to Nifty/BankNifty spot + ATM options on WebSocket
   - Send "🟢 System Online" WhatsApp message

2. SCANNING LOOP (every 5 minutes, 9:20 AM - 3:15 PM):
   - Fetch option chain data
   - Run options scanner
   - If signal found AND risk manager approves:
     a. Format trade alert message
     b. Send to WhatsApp
     c. Store signal in DB with PENDING status
     d. Start 5-minute confirmation timer

3. CONFIRMATION HANDLER (webhook-driven):
   - On "YES": execute order, start exit monitor
   - On "NO": mark signal as SKIPPED
   - On timeout: mark signal as EXPIRED

4. POSITION MONITORING (continuous while positions open):
   - Check SL/target/trailing SL every tick
   - Execute exit when conditions met
   - Send exit notification on WhatsApp

5. END OF DAY (3:20 PM):
   - Square off any remaining MIS positions
   - Send daily P&L summary on WhatsApp
   - Send "🔴 System Offline" message
   - Store daily stats in DB

6. ERROR HANDLING:
   - If Kite disconnects → auto-reconnect, send alert
   - If WebSocket drops → resubscribe
   - If order fails → send error alert, do NOT retry automatically
   - If daily loss limit hit → stop all trading, send alert
"""
```

---

## 7. Database Schema (`database/models.py`)

```sql
-- Signals generated by scanner/AI
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    underlying TEXT NOT NULL,           -- 'NIFTY' or 'BANKNIFTY'
    tradingsymbol TEXT NOT NULL,        -- 'NIFTY2530622500CE'
    action TEXT NOT NULL,               -- 'BUY' or 'SELL'
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    target REAL NOT NULL,
    quantity INTEGER NOT NULL,
    reasoning TEXT,
    confidence REAL,
    status TEXT DEFAULT 'PENDING',      -- PENDING, CONFIRMED, REJECTED, EXPIRED, EXECUTED
    whatsapp_sent_at TIMESTAMP,
    confirmed_at TIMESTAMP,
    expired_at TIMESTAMP
);

-- Executed trades
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    entry_order_id TEXT NOT NULL,
    exit_order_id TEXT,
    tradingsymbol TEXT NOT NULL,
    action TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    quantity INTEGER NOT NULL,
    stop_loss REAL NOT NULL,
    target REAL NOT NULL,
    entry_time TIMESTAMP NOT NULL,
    exit_time TIMESTAMP,
    exit_reason TEXT,                   -- 'SL_HIT', 'TARGET_HIT', 'TRAILING_SL', 'TIME_EXIT', 'MANUAL', 'EOD_SQUAREOFF'
    pnl REAL,
    charges REAL,                       -- brokerage + STT + stamp + GST
    net_pnl REAL,
    status TEXT DEFAULT 'OPEN'          -- OPEN, CLOSED, ERROR
);

-- Daily performance log
CREATE TABLE daily_summary (
    date TEXT PRIMARY KEY,
    total_trades INTEGER,
    winners INTEGER,
    losers INTEGER,
    gross_pnl REAL,
    net_pnl REAL,
    max_drawdown REAL,
    capital_used REAL
);

-- Session tokens
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    access_token TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    valid_until TIMESTAMP
);
```

---

## 8. Configuration Defaults (`config/settings.py`)

```python
# ─── Scanning Config ───
SCAN_INTERVAL_SECONDS = 300          # 5 minutes
SCAN_UNDERLYINGS = ["NIFTY", "BANKNIFTY"]
STRIKES_RANGE = 10                   # ATM ± 10 strikes
USE_AI_ANALYSIS = True               # Use Claude for analysis (set False for rule-based only)

# ─── Risk Management ───
MAX_LOSS_PER_TRADE = 2000            # ₹
MAX_LOSS_PER_DAY = 5000              # ₹
MAX_OPEN_POSITIONS = 2
MAX_CAPITAL_PERCENT_PER_TRADE = 0.05 # 5%
MIN_RISK_REWARD_RATIO = 1.5
MIN_OPTION_PREMIUM = 50              # Don't trade options below ₹50 (wide spreads)
MAX_BID_ASK_SPREAD_PERCENT = 2.0     # Skip if bid-ask > 2%

# ─── Exit Config ───
TRAILING_SL_TRIGGER_PERCENT = 50     # Start trailing after 50% of target reached
TRAILING_SL_BREAKEVEN_PERCENT = 50   # Trail to breakeven at 50%
TRAILING_SL_PROFIT_LOCK_PERCENT = 75 # Lock 50% profit at 75% of target
MAX_HOLDING_MINUTES = 120            # Exit if held > 2 hours with < 10% profit
EOD_EXIT_TIME = "15:15"              # Square off MIS by 3:15 PM
EXPIRY_DAY_EXIT_TIME = "15:20"       # Earlier exit on expiry day

# ─── WhatsApp Config ───
TRADE_CONFIRMATION_TIMEOUT_SECONDS = 300  # 5 minutes to confirm
SEND_DAILY_SUMMARY = True
DAILY_SUMMARY_TIME = "15:35"         # Send summary at 3:35 PM

# ─── Market Hours ───
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
PRE_MARKET_LOGIN = "09:00"
SCAN_START = "09:20"                 # Skip first 5 min volatility
SCAN_END = "15:15"

# ─── Charges (approximate, for P&L calculation) ───
BROKERAGE_PER_ORDER = 20             # ₹20 flat per executed order (Zerodha)
STT_PERCENT_SELL = 0.0625            # STT on sell side for options
GST_PERCENT = 0.18                   # 18% GST on brokerage
STAMP_DUTY_PERCENT = 0.003           # Stamp duty on buy side
SEBI_CHARGES_PERCENT = 0.0001        # SEBI turnover charges
EXCHANGE_TXN_PERCENT = 0.00053       # NSE transaction charges
```

---

## 9. Setup Instructions

### 9.1 Zerodha Kite Connect Setup

1. **Have an active Zerodha trading account** with F&O enabled
2. Go to https://developers.kite.trade/login and create a developer account
3. Click "Create App" → fill in app name, your Zerodha client ID, and redirect URL (use your server URL like `https://your-domain.com/kite/callback`)
4. Note down `api_key` and `api_secret`
5. **Enable TOTP** on your Zerodha account (Kite > Settings > Security > Enable TOTP)
6. When setting up TOTP, save the **base32 secret key** (not the QR code) — this goes in `KITE_TOTP_SECRET`
7. **Kite Connect costs ₹2,000/month** — subscribe at https://developers.kite.trade

### 9.2 Twilio WhatsApp Setup

1. Create account at https://www.twilio.com
2. Go to Console → Messaging → Try it out → Send a WhatsApp message
3. Join the **Twilio sandbox**: Send "join <sandbox-word>" to the Twilio WhatsApp number from your phone
4. Note down `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and the sandbox WhatsApp number
5. Set webhook URL in Twilio Console → WhatsApp Sandbox → "When a message comes in" → `https://your-server.com/whatsapp/webhook`
6. For production: Apply for a dedicated WhatsApp Business number through Twilio

### 9.3 Python Environment

```bash
python -m venv venv
source venv/bin/activate
pip install kiteconnect pyotp twilio flask apscheduler anthropic \
            numpy scipy pandas requests python-dotenv
```

---

## 10. Key Implementation Notes

### Kite Connect API Specifics

- **Rate limits:** 10 requests/second for most endpoints. Historical data: 3 req/sec. Orders: 10 req/sec, max 200/min.
- **WebSocket:** Max 3000 instrument tokens per connection. Mode FULL gives OI + depth.
- **Instruments list:** Call `kite.instruments("NFO")` once daily at startup. Cache it. It returns ALL F&O instruments with token, tradingsymbol, lot_size, expiry, strike, instrument_type.
- **Option chain construction:** Filter instruments list by underlying + expiry + instrument_type to build the chain. Then call `kite.quote()` or WebSocket for live prices.
- **Session validity:** Access token expires at ~6 AM next day. Login once per day at pre-market.
- **Order varieties:** Use `VARIETY_REGULAR` for normal orders. `VARIETY_BO` for bracket orders (auto SL + target). `VARIETY_CO` for cover orders.
- **Product types:** `PRODUCT_MIS` for intraday (auto square-off at 3:20 PM). `PRODUCT_NRML` for positional.

### Greeks Calculation

The exchange provides IV in full-mode ticks, but NOT delta/gamma/theta/vega. You must compute these yourself using Black-Scholes:

```python
from scipy.stats import norm
import numpy as np

def black_scholes_greeks(S, K, T, r, sigma, option_type='CE'):
    """
    S: spot price, K: strike, T: time to expiry (years), 
    r: risk-free rate, sigma: IV (decimal), option_type: 'CE'/'PE'
    """
    d1 = (np.log(S/K) + (r + sigma**2/2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    
    if option_type == 'CE':
        delta = norm.cdf(d1)
        theta = (-(S*norm.pdf(d1)*sigma)/(2*np.sqrt(T)) - r*K*np.exp(-r*T)*norm.cdf(d2)) / 365
    else:
        delta = norm.cdf(d1) - 1
        theta = (-(S*norm.pdf(d1)*sigma)/(2*np.sqrt(T)) + r*K*np.exp(-r*T)*norm.cdf(-d2)) / 365
    
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))
    vega = S * norm.pdf(d1) * np.sqrt(T) / 100
    
    return {'delta': delta, 'gamma': gamma, 'theta': theta, 'vega': vega}
```

### Thread Safety

The system runs multiple concurrent tasks:
- WebSocket tick receiver (background thread)
- Flask webhook server (separate thread/process)
- Scanner loop (main thread or scheduled)
- Exit monitor (per-position thread)

Use `threading.Lock()` for shared state (pending signals, active trades). Consider using `queue.Queue` for passing signals between scanner and WhatsApp sender.

### Error Recovery

- **Kite WebSocket disconnect:** `KiteTicker` has built-in reconnect. Set `kws.on_reconnect` callback to resubscribe.
- **Order rejection:** Check `kite.order_history(order_id)` for rejection reason. Common: insufficient margin, invalid price, market closed.
- **Flask crash:** Use gunicorn with auto-restart in production.
- **Daily login failure:** Retry 3 times with 30-second gaps. If all fail, send WhatsApp error alert.

---

## 11. Testing Plan

1. **Paper Trading First:** Use Kite Connect's test credentials and don't place real orders. Log everything as if real.
2. **Backtest strategies** on historical data before going live. Use `kite.historical_data()` for past OHLC.
3. **Simulate WhatsApp flow** using Twilio sandbox.
4. **Start with minimum lot size** (1 lot) and `MAX_LOSS_PER_DAY = ₹1000` for first 2 weeks.
5. **Monitor for at least 1 month** before increasing position size.

---

## 12. Regulatory Notes

- **SEBI Algo Trading Rules:** For personal use, building your own algo is fine. If you plan to offer this as a service to others, you need SEBI registration as a Research Analyst and exchange approval for the algo.
- **Zerodha's Policy:** They allow API-based trading for personal accounts. Don't share your API credentials.
- **Risk Disclosure:** Options trading involves substantial risk. This system should be treated as a tool, not guaranteed profit. Always trade with capital you can afford to lose.

---

## 13. Future Enhancements

- Add Telegram as alternative notification channel
- Multi-broker support (Dhan, Fyers as backup)
- Dashboard web UI for monitoring (Flask + React)
- Backtesting engine with historical option chain data
- Sentiment analysis from news/Twitter
- Multi-strategy support with allocation weights
- Paper trading mode toggle in WhatsApp ("MODE PAPER" / "MODE LIVE")
