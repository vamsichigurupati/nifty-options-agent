#!/usr/bin/env python3
"""
Tiny web server to update Dhan access token remotely.

Access from phone/browser:
  https://<SERVER_IP>:8080/

  Enter your secret key + new Dhan token, hit Update.
  The paper trading service restarts automatically.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from flask import Flask, request, Response

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

ENV_PATH = Path(__file__).parent.parent / ".env"
SECRET_KEY = os.getenv("TOKEN_UPDATE_SECRET", "nifty2026")  # change this in .env


@app.route("/", methods=["GET"])
def home():
    """Simple HTML form to update token."""
    last_update = ""
    token_file = Path(__file__).parent.parent / "data" / "last_token_update.txt"
    if token_file.exists():
        last_update = token_file.read_text().strip()

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dhan Token Update</title>
<style>
body{{font-family:system-ui;background:#0a0e1a;color:#e0e0e0;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;}}
.card{{background:#111827;border:1px solid #1f2937;border-radius:16px;padding:30px;max-width:400px;width:90%;}}
h2{{color:#00d4ff;margin:0 0 20px;text-align:center;}}
label{{display:block;font-size:0.85em;color:#9ca3af;margin:12px 0 4px;}}
input{{width:100%;padding:10px;background:#0a0f1e;border:1px solid #253050;color:#e0e0e0;border-radius:8px;font-size:0.9em;box-sizing:border-box;}}
textarea{{width:100%;padding:10px;background:#0a0f1e;border:1px solid #253050;color:#e0e0e0;border-radius:8px;font-size:0.8em;height:80px;box-sizing:border-box;font-family:monospace;}}
button{{width:100%;padding:12px;background:#3b82f6;color:#fff;border:none;border-radius:8px;font-size:1em;font-weight:700;cursor:pointer;margin-top:16px;}}
button:hover{{background:#2563eb;}}
.status{{text-align:center;font-size:0.8em;color:#6b7280;margin-top:12px;}}
.ok{{color:#10b981;}} .err{{color:#ef4444;}}
</style></head><body>
<div class="card">
<h2>Dhan Token Update</h2>
<form method="POST" action="/update">
<label>Secret Key</label>
<input type="password" name="secret" required placeholder="Your secret key">
<label>New Dhan Access Token</label>
<textarea name="token" required placeholder="Paste Dhan access token here..."></textarea>
<button type="submit">Update Token & Restart</button>
</form>
<div class="status">Last update: {last_update or 'Never'}</div>
</div></body></html>"""


@app.route("/update", methods=["POST"])
def update_token():
    """Update the token in .env and restart the service."""
    secret = request.form.get("secret", "")
    token = request.form.get("token", "").strip()

    if secret != SECRET_KEY:
        return _response("Invalid secret key.", "err")

    if not token or len(token) < 50:
        return _response("Token too short. Paste the full Dhan access token.", "err")

    # Extract client ID from JWT
    try:
        import base64, json
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.b64decode(payload))
        client_id = data.get("dhanClientId", "")
    except Exception:
        client_id = ""

    # Update .env
    try:
        if ENV_PATH.exists():
            content = ENV_PATH.read_text()
            lines = content.split("\n")
            new_lines = []
            token_updated = False
            client_updated = False
            for line in lines:
                if line.startswith("DHAN_ACCESS_TOKEN="):
                    new_lines.append(f"DHAN_ACCESS_TOKEN={token}")
                    token_updated = True
                elif line.startswith("DHAN_CLIENT_ID=") and client_id:
                    new_lines.append(f"DHAN_CLIENT_ID={client_id}")
                    client_updated = True
                else:
                    new_lines.append(line)
            if not token_updated:
                new_lines.append(f"DHAN_ACCESS_TOKEN={token}")
            if not client_updated and client_id:
                new_lines.append(f"DHAN_CLIENT_ID={client_id}")
            ENV_PATH.write_text("\n".join(new_lines))
        else:
            ENV_PATH.write_text(f"DHAN_CLIENT_ID={client_id}\nDHAN_ACCESS_TOKEN={token}\n")

        # Log the update time
        log_path = Path(__file__).parent.parent / "data" / "last_token_update.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        # Restart the paper trading service
        try:
            subprocess.run(["sudo", "systemctl", "restart", "nifty-agent"],
                           capture_output=True, timeout=10)
            restart_msg = "Service restarted."
        except Exception:
            restart_msg = "Token saved. Restart the service manually."

        logger.info(f"Token updated. Client: {client_id}")
        return _response(f"Token updated successfully! Client ID: {client_id}. {restart_msg}", "ok")

    except Exception as e:
        logger.error(f"Token update failed: {e}")
        return _response(f"Error: {e}", "err")


@app.route("/status", methods=["GET"])
def status():
    """Quick status check."""
    token_file = Path(__file__).parent.parent / "data" / "last_token_update.txt"
    last = token_file.read_text().strip() if token_file.exists() else "Never"

    # Check if service is running
    try:
        result = subprocess.run(["systemctl", "is-active", "nifty-agent"],
                                capture_output=True, text=True, timeout=5)
        service = result.stdout.strip()
    except Exception:
        service = "unknown"

    return {"status": "ok", "service": service, "last_token_update": last}


@app.route("/trades", methods=["GET"])
def trades_page():
    """View all paper trades from phone."""
    from paper_trading.versions import VERSION_CONFIG
    trades_dir = Path(__file__).parent.parent / "data" / "dhan_paper_trades"

    versions = {}
    for vid, cfg in VERSION_CONFIG.items():
        path = trades_dir / f"{vid}.json"
        if path.exists():
            with open(path) as f:
                state = json.load(f)
            capital = state.get("capital", 50000)
            initial = state.get("initial_capital", 50000)
            all_trades = state.get("trades", [])
            open_trade = state.get("open_trade")
            closed = [t for t in all_trades if t.get("status") == "CLOSED"]
            winners = [t for t in closed if (t.get("pnl") or 0) > 0]
            pnl = capital - initial
            wr = round(len(winners) / max(len(closed), 1) * 100, 1)
            versions[vid] = {
                "name": cfg["name"], "color": cfg["color"],
                "capital": capital, "pnl": pnl, "roi": round(pnl / initial * 100, 1),
                "trades": len(closed), "winners": len(winners),
                "losers": len(closed) - len(winners), "win_rate": wr,
                "closed": closed, "open_trade": open_trade,
            }

    # Build HTML
    cards = ""
    for vid, v in versions.items():
        pnl_cls = "pos" if v["pnl"] >= 0 else "neg"
        open_html = ""
        if v["open_trade"]:
            ot = v["open_trade"]
            open_html = (f'<div style="background:#172554;border:1px solid #3b82f6;border-radius:8px;'
                         f'padding:10px;margin:8px 0;font-size:0.82em;">'
                         f'OPEN: {ot.get("symbol","")} {ot.get("type","")} @ {ot.get("entry_price",0):.2f} '
                         f'SL={ot.get("stop_loss",0):.2f} TGT={ot.get("target",0):.2f}</div>')

        trade_rows = ""
        for t in reversed(v["closed"][-20:]):
            p = t.get("pnl", 0)
            cls = "pos" if p > 0 else "neg"
            entry = t.get("entry_time", "")[:16]
            trade_rows += (f'<tr><td>{t.get("id","")}</td><td>{entry}</td>'
                          f'<td>{t.get("symbol","")[:16]}</td>'
                          f'<td class="{cls}">{p:+,.0f}</td>'
                          f'<td>{t.get("exit_reason","")}</td></tr>')

        cards += f"""<div class="card" style="border-top:3px solid {v['color']};">
<h3 style="color:{v['color']};margin:0 0 10px;">{v['name']}</h3>
<div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px;text-align:center;">
<div><div class="v {pnl_cls}">{v['pnl']:+,.0f}</div><div class="l">P&L</div></div>
<div><div class="v {pnl_cls}">{v['roi']:+.1f}%</div><div class="l">ROI</div></div>
<div><div class="v">{v['trades']}</div><div class="l">Trades</div></div>
<div><div class="v">{v['win_rate']}%</div><div class="l">Win Rate</div></div>
</div>
{open_html}
<table style="width:100%;border-collapse:collapse;font-size:0.78em;margin-top:10px;">
<tr style="background:#1a2240;"><th style="padding:6px;text-align:left;color:#00d4ff;">#</th>
<th style="padding:6px;text-align:left;color:#00d4ff;">Time</th>
<th style="padding:6px;text-align:left;color:#00d4ff;">Symbol</th>
<th style="padding:6px;text-align:left;color:#00d4ff;">P&L</th>
<th style="padding:6px;text-align:left;color:#00d4ff;">Exit</th></tr>
{trade_rows if trade_rows else '<tr><td colspan="5" style="padding:10px;color:#6b7280;">No trades yet</td></tr>'}
</table></div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Trading Dashboard</title>
<meta http-equiv="refresh" content="120">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:system-ui;background:#0a0e1a;color:#e0e0e0;padding:16px;}}
h2{{text-align:center;color:#00d4ff;margin-bottom:16px;font-size:1.3em;}}
.card{{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:16px;margin:12px 0;}}
.card h3{{font-size:1em;}}
.v{{font-size:1.2em;font-weight:800;}} .l{{font-size:0.65em;color:#6b7280;}}
.pos{{color:#10b981;}} .neg{{color:#ef4444;}}
table td{{padding:5px 6px;border-bottom:1px solid #1a2040;}}
a{{color:#3b82f6;text-decoration:none;}}
.nav{{text-align:center;margin:12px 0;font-size:0.85em;}}
</style></head><body>
<h2>Paper Trading</h2>
<div class="nav"><a href="/">Update Token</a> | <a href="/trades">Trades</a> | <a href="/status">Status</a></div>
{cards if cards else '<div class="card"><p style="color:#6b7280;text-align:center;">No trading data yet. System is accumulating candles.</p></div>'}
<p style="text-align:center;color:#374151;font-size:0.7em;margin-top:16px;">Auto-refreshes every 2 minutes</p>
</body></html>"""


@app.route("/update-token", methods=["GET"])
def update_token_url():
    """URL-based update (for bookmarks/shortcuts)."""
    key = request.args.get("key", "")
    token = request.args.get("token", "")

    if key != SECRET_KEY:
        return {"status": "error", "message": "Invalid key"}
    if not token or len(token) < 50:
        return {"status": "error", "message": "Invalid token"}

    # Reuse the POST logic
    with app.test_request_context("/update", method="POST",
                                   data={"secret": key, "token": token}):
        return update_token()


def _response(msg, cls):
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:system-ui;background:#0a0e1a;color:#e0e0e0;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;}}
.card{{background:#111827;border:1px solid #1f2937;border-radius:16px;padding:30px;max-width:400px;width:90%;text-align:center;}}
.ok{{color:#10b981;}} .err{{color:#ef4444;}}
a{{color:#3b82f6;}}</style></head><body>
<div class="card"><h2 class="{cls}">{'Done!' if cls == 'ok' else 'Error'}</h2>
<p>{msg}</p><br><a href="/">Back</a></div></body></html>"""


if __name__ == "__main__":
    port = int(os.getenv("TOKEN_SERVER_PORT", "8080"))
    print(f"Token update server running on port {port}")
    print(f"Open: http://localhost:{port}/")
    app.run(host="0.0.0.0", port=port, debug=False)
