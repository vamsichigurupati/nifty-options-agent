from datetime import datetime


def format_trade_alert(signal) -> str:
    """Format a trade signal into a WhatsApp alert message."""
    sl_pct = ((signal["stop_loss"] - signal["entry_price"]) / signal["entry_price"]) * 100
    tgt_pct = ((signal["target"] - signal["entry_price"]) / signal["entry_price"]) * 100
    risk = abs(signal["entry_price"] - signal["stop_loss"])
    reward = abs(signal["target"] - signal["entry_price"])
    rr = reward / risk if risk > 0 else 0
    capital = signal["entry_price"] * signal["quantity"]

    return (
        f"\U0001F514 *TRADE ALERT*\n"
        f"{'=' * 20}\n"
        f"\U0001F4CA {signal['tradingsymbol']}\n"
        f"\U0001F4C5 Expiry: {signal.get('expiry', 'N/A')}\n\n"
        f"\u25B6\uFE0F Action: {signal['action']}\n"
        f"\U0001F4B0 Entry: \u20B9{signal['entry_price']:.2f}\n"
        f"\U0001F6D1 Stop Loss: \u20B9{signal['stop_loss']:.2f} ({sl_pct:+.1f}%)\n"
        f"\U0001F3AF Target: \u20B9{signal['target']:.2f} ({tgt_pct:+.1f}%)\n"
        f"\U0001F4D0 Risk:Reward: 1:{rr:.2f}\n"
        f"\U0001F4E6 Qty: {signal['quantity']}\n"
        f"\U0001F4B5 Capital Required: ~\u20B9{capital:,.0f}\n\n"
        f"\U0001F4DD Reason: {signal.get('reasoning', 'N/A')}\n\n"
        f"\u2705 Reply YES to execute\n"
        f"\u274C Reply NO to skip\n"
        f"\u23F0 Alert expires in 5 minutes"
    )


def format_execution_confirmation(order_id: str, details: dict) -> str:
    return (
        f"\u2705 *ORDER EXECUTED*\n"
        f"Order ID: {order_id}\n"
        f"{details['tradingsymbol']} {details['action']} @ \u20B9{details['price']:.2f}\n"
        f"Qty: {details['quantity']} | Product: {details.get('product', 'MIS')}\n"
        f"SL order placed at \u20B9{details['stop_loss']:.2f}"
    )


def format_exit_notification(trade: dict) -> str:
    entry = trade["entry_price"]
    exit_p = trade.get("exit_price", 0)
    pnl = trade.get("net_pnl", 0) or 0
    pnl_pct = ((exit_p - entry) / entry * 100) if entry > 0 else 0
    emoji = "\U0001F4C8" if pnl >= 0 else "\U0001F4C9"

    entry_time = datetime.fromisoformat(trade["entry_time"])
    exit_time = datetime.fromisoformat(trade.get("exit_time", datetime.now().isoformat()))
    duration = exit_time - entry_time
    mins = int(duration.total_seconds() / 60)

    return (
        f"{emoji} *POSITION CLOSED*\n"
        f"{'=' * 20}\n"
        f"{trade['tradingsymbol']}\n"
        f"Entry: \u20B9{entry:.2f} \u2192 Exit: \u20B9{exit_p:.2f}\n"
        f"P&L: {'+' if pnl >= 0 else ''}\u20B9{pnl:,.2f} ({pnl_pct:+.1f}%)\n"
        f"Exit Reason: {trade.get('exit_reason', 'N/A')}\n"
        f"Duration: {mins} minutes"
    )


def format_daily_summary(trades: list[dict]) -> str:
    if not trades:
        return "\U0001F4CA *DAILY SUMMARY*\nNo trades today."

    closed = [t for t in trades if t["status"] == "CLOSED"]
    winners = [t for t in closed if (t.get("net_pnl") or 0) > 0]
    losers = [t for t in closed if (t.get("net_pnl") or 0) <= 0]
    gross_pnl = sum(t.get("pnl") or 0 for t in closed)
    net_pnl = sum(t.get("net_pnl") or 0 for t in closed)
    capital = sum(t["entry_price"] * t["quantity"] for t in closed) if closed else 0
    roi = (net_pnl / capital * 100) if capital > 0 else 0

    return (
        f"\U0001F4CA *DAILY SUMMARY*\n"
        f"{'=' * 20}\n"
        f"Total Trades: {len(closed)}\n"
        f"Winners: {len(winners)} | Losers: {len(losers)}\n"
        f"Gross P&L: {'+' if gross_pnl >= 0 else ''}\u20B9{gross_pnl:,.0f}\n"
        f"Net P&L: {'+' if net_pnl >= 0 else ''}\u20B9{net_pnl:,.0f}\n"
        f"Capital Used: \u20B9{capital:,.0f}\n"
        f"ROI: {roi:.1f}%"
    )


def format_positions(trades: list[dict]) -> str:
    if not trades:
        return "No open positions."

    lines = ["\U0001F4CB *OPEN POSITIONS*\n"]
    for t in trades:
        pnl = (t.get("exit_price", t["entry_price"]) - t["entry_price"]) * t["quantity"]
        lines.append(
            f"- {t['tradingsymbol']}: {t['action']} @ \u20B9{t['entry_price']:.2f} "
            f"| P&L: {'+' if pnl >= 0 else ''}\u20B9{pnl:,.0f}"
        )
    return "\n".join(lines)


def format_error(error: str) -> str:
    return f"\u26A0\uFE0F *SYSTEM ERROR*\n{error}"


def format_system_status(status: str) -> str:
    if status == "online":
        return "\U0001F7E2 *System Online* - Trading agent active"
    elif status == "offline":
        return "\U0001F534 *System Offline* - Trading stopped for the day"
    elif status == "stopped":
        return "\u23F9\uFE0F *Trading Stopped* - Manual stop activated"
    return f"System status: {status}"
