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
