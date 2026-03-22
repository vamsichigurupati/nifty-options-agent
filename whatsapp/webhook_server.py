import logging
import threading
from flask import Flask, request, Response
from config.settings import USER_WHATSAPP_TO, FLASK_PORT

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Shared state — set by main.py
_trade_handler = None
_handler_lock = threading.Lock()


def set_trade_handler(handler):
    """Set the callback for handling trade confirmations.

    handler signature: handler(command: str) -> str
    Commands: YES, NO, STATUS, STOP, POSITIONS, EXIT <symbol>, EXIT ALL
    Returns: response message string
    """
    global _trade_handler
    with _handler_lock:
        _trade_handler = handler


@app.route("/whatsapp/webhook", methods=["POST"])
def whatsapp_webhook():
    """Handle incoming WhatsApp messages from Twilio."""
    sender = request.form.get("From", "")
    body = request.form.get("Body", "").strip().upper()

    # Verify sender is the authorized user
    if sender != USER_WHATSAPP_TO:
        logger.warning(f"Unauthorized WhatsApp from: {sender}")
        return Response(status=403)

    logger.info(f"WhatsApp received: '{body}' from {sender}")

    with _handler_lock:
        handler = _trade_handler

    if handler is None:
        logger.warning("No trade handler registered")
        return _twiml_response("System not ready. Please try again.")

    try:
        response_text = handler(body)
    except Exception as e:
        logger.error(f"Handler error: {e}")
        response_text = f"Error processing command: {str(e)}"

    return _twiml_response(response_text)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


def _twiml_response(message: str) -> Response:
    """Return a TwiML response."""
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Message>{message}</Message>"
        "</Response>"
    )
    return Response(twiml, mimetype="application/xml")


def start_webhook_server():
    """Start Flask server in a background thread."""
    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=FLASK_PORT, debug=False),
        daemon=True,
    )
    thread.start()
    logger.info(f"Webhook server started on port {FLASK_PORT}")
    return thread
