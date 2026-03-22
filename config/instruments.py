NIFTY_SPOT_TOKEN = 256265       # NSE:NIFTY 50
BANKNIFTY_SPOT_TOKEN = 260105   # NSE:NIFTY BANK

UNDERLYING_MAP = {
    "NIFTY": {
        "spot_token": NIFTY_SPOT_TOKEN,
        "exchange": "NSE",
        "tradingsymbol": "NIFTY 50",
        "nfo_prefix": "NIFTY",
        "strike_step": 50,
    },
    "BANKNIFTY": {
        "spot_token": BANKNIFTY_SPOT_TOKEN,
        "exchange": "NSE",
        "tradingsymbol": "NIFTY BANK",
        "nfo_prefix": "BANKNIFTY",
        "strike_step": 100,
    },
}
