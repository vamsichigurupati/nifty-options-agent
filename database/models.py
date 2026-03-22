SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    underlying TEXT NOT NULL,
    tradingsymbol TEXT NOT NULL,
    action TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    target REAL NOT NULL,
    quantity INTEGER NOT NULL,
    reasoning TEXT,
    confidence REAL,
    status TEXT DEFAULT 'PENDING',
    whatsapp_sent_at TIMESTAMP,
    confirmed_at TIMESTAMP,
    expired_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
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
    exit_reason TEXT,
    pnl REAL,
    charges REAL,
    net_pnl REAL,
    status TEXT DEFAULT 'OPEN'
);

CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,
    total_trades INTEGER,
    winners INTEGER,
    losers INTEGER,
    gross_pnl REAL,
    net_pnl REAL,
    max_drawdown REAL,
    capital_used REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    access_token TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    valid_until TIMESTAMP
);
"""
