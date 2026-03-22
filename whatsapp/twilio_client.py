import logging
from twilio.rest import Client
from config.settings import (
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP_FROM, USER_WHATSAPP_TO
)
from whatsapp.message_templates import (
    format_trade_alert, format_execution_confirmation,
    format_exit_notification, format_daily_summary,
    format_positions, format_error, format_system_status
)

logger = logging.getLogger(__name__)


class WhatsAppNotifier:
    def __init__(self):
        self.client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        self.from_number = TWILIO_WHATSAPP_FROM
        self.to_number = USER_WHATSAPP_TO

    def _send(self, body: str):
        """Send a WhatsApp message."""
        try:
            message = self.client.messages.create(
                body=body,
                from_=self.from_number,
                to=self.to_number,
            )
            logger.info(f"WhatsApp sent: {message.sid}")
            return message.sid
        except Exception as e:
            logger.error(f"WhatsApp send failed: {e}")
            return None

    def send_trade_alert(self, signal: dict):
        body = format_trade_alert(signal)
        return self._send(body)

    def send_execution_confirmation(self, order_id: str, details: dict):
        body = format_execution_confirmation(order_id, details)
        return self._send(body)

    def send_exit_notification(self, trade: dict):
        body = format_exit_notification(trade)
        return self._send(body)

    def send_daily_summary(self, trades: list[dict]):
        body = format_daily_summary(trades)
        return self._send(body)

    def send_positions(self, trades: list[dict]):
        body = format_positions(trades)
        return self._send(body)

    def send_error_alert(self, error: str):
        body = format_error(error)
        return self._send(body)

    def send_system_status(self, status: str):
        body = format_system_status(status)
        return self._send(body)
