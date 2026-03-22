import json
import logging
import anthropic
from config.settings import ANTHROPIC_API_KEY
from ai.prompts import SYSTEM_PROMPT, build_analysis_prompt, build_exit_prompt

logger = logging.getLogger(__name__)


class TradeAnalyzer:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.model = "claude-sonnet-4-20250514"

    def analyze_market(self, context: dict) -> dict | None:
        """Send market snapshot to Claude for analysis.

        Args:
            context: dict with keys: underlying, spot_price, days_to_expiry,
                     top_options (list of dicts), max_risk, min_rr

        Returns parsed JSON recommendation or None.
        """
        user_prompt = build_analysis_prompt(context)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            text = response.content[0].text.strip()
            # Parse JSON from response (handle potential markdown wrapping)
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            result = json.loads(text)
            logger.info(f"AI analysis: {result.get('action')} - {result.get('reasoning', '')[:80]}")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI response: {e}")
            return None
        except Exception as e:
            logger.error(f"AI analysis failed: {e}")
            return None

    def evaluate_exit(self, position: dict, market_data: dict) -> dict | None:
        """Ask Claude whether to hold, exit, or adjust."""
        user_prompt = build_exit_prompt(position, market_data)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            return json.loads(text)

        except Exception as e:
            logger.error(f"AI exit evaluation failed: {e}")
            return None
