import sqlite3
import os
from datetime import datetime, timedelta
from config.settings import DATABASE_PATH
from database.models import SCHEMA_SQL


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()


# --- Signal Operations ---

def insert_signal(underlying, tradingsymbol, action, entry_price, stop_loss,
                  target, quantity, reasoning=None, confidence=None) -> int:
    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO signals (underlying, tradingsymbol, action, entry_price,
           stop_loss, target, quantity, reasoning, confidence, whatsapp_sent_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (underlying, tradingsymbol, action, entry_price, stop_loss, target,
         quantity, reasoning, confidence, datetime.now().isoformat())
    )
    signal_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return signal_id


def update_signal_status(signal_id: int, status: str):
    conn = get_connection()
    now = datetime.now().isoformat()
    if status == "CONFIRMED":
        conn.execute("UPDATE signals SET status=?, confirmed_at=? WHERE id=?",
                      (status, now, signal_id))
    elif status == "EXPIRED":
        conn.execute("UPDATE signals SET status=?, expired_at=? WHERE id=?",
                      (status, now, signal_id))
    else:
        conn.execute("UPDATE signals SET status=? WHERE id=?", (status, signal_id))
    conn.commit()
    conn.close()


def get_pending_signal():
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM signals WHERE status='PENDING' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_signal(signal_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# --- Trade Operations ---

def insert_trade(signal_id, entry_order_id, tradingsymbol, action, entry_price,
                 quantity, stop_loss, target) -> int:
    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO trades (signal_id, entry_order_id, tradingsymbol, action,
           entry_price, quantity, stop_loss, target, entry_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (signal_id, entry_order_id, tradingsymbol, action, entry_price,
         quantity, stop_loss, target, datetime.now().isoformat())
    )
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def close_trade(trade_id: int, exit_order_id: str, exit_price: float,
                exit_reason: str, pnl: float, charges: float):
    conn = get_connection()
    conn.execute(
        """UPDATE trades SET exit_order_id=?, exit_price=?, exit_time=?,
           exit_reason=?, pnl=?, charges=?, net_pnl=?, status='CLOSED'
           WHERE id=?""",
        (exit_order_id, exit_price, datetime.now().isoformat(),
         exit_reason, pnl, charges, pnl - charges, trade_id)
    )
    conn.commit()
    conn.close()


def get_open_trades() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_trade(trade_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_todays_trades() -> list[dict]:
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT * FROM trades WHERE date(entry_time)=?", (today,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_pnl() -> float:
    trades = get_todays_trades()
    return sum(t.get("net_pnl", 0) or 0 for t in trades if t["status"] == "CLOSED")


# --- Daily Summary ---

def save_daily_summary(total_trades, winners, losers, gross_pnl, net_pnl,
                       max_drawdown, capital_used):
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """INSERT OR REPLACE INTO daily_summary
           (date, total_trades, winners, losers, gross_pnl, net_pnl,
            max_drawdown, capital_used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (today, total_trades, winners, losers, gross_pnl, net_pnl,
         max_drawdown, capital_used)
    )
    conn.commit()
    conn.close()


# --- Session Operations ---

def save_session(access_token: str, valid_hours: int = 24):
    conn = get_connection()
    valid_until = (datetime.now() + timedelta(hours=valid_hours)).isoformat()
    conn.execute("DELETE FROM sessions")
    conn.execute(
        "INSERT INTO sessions (id, access_token, valid_until) VALUES (1, ?, ?)",
        (access_token, valid_until)
    )
    conn.commit()
    conn.close()


def get_session():
    conn = get_connection()
    row = conn.execute("SELECT * FROM sessions WHERE id=1").fetchone()
    conn.close()
    if row:
        row = dict(row)
        if datetime.fromisoformat(row["valid_until"]) > datetime.now():
            return row
    return None
