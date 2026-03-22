import logging
import time
import pyotp
import requests
from kiteconnect import KiteConnect
from config.settings import (
    KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET
)
from database.db import save_session, get_session

logger = logging.getLogger(__name__)

LOGIN_URL = "https://kite.zerodha.com/api/login"
TWOFA_URL = "https://kite.zerodha.com/api/twofa"


class KiteAuth:
    def __init__(self):
        self.kite = KiteConnect(api_key=KITE_API_KEY)

    def _generate_totp(self) -> str:
        totp = pyotp.TOTP(KITE_TOTP_SECRET)
        return totp.now()

    def login(self) -> str:
        """Perform full Kite login flow and return access_token."""
        session = requests.Session()

        # Step 1: POST credentials
        resp = session.post(LOGIN_URL, data={
            "user_id": KITE_USER_ID,
            "password": KITE_PASSWORD,
        })
        resp.raise_for_status()
        data = resp.json().get("data", {})
        request_id = data.get("request_id")
        if not request_id:
            raise RuntimeError(f"Login step 1 failed: {resp.json()}")

        # Step 2: POST TOTP
        totp_code = self._generate_totp()
        resp = session.post(TWOFA_URL, data={
            "user_id": KITE_USER_ID,
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp",
        })
        resp.raise_for_status()

        # Extract request_token from redirect
        redirect_url = resp.url if resp.history else resp.headers.get("Location", "")
        if "request_token=" not in redirect_url:
            # Try from response JSON for newer API versions
            data = resp.json().get("data", {})
            request_token = data.get("request_token")
            if not request_token:
                raise RuntimeError(f"Could not extract request_token: {resp.text}")
        else:
            request_token = redirect_url.split("request_token=")[1].split("&")[0]

        # Step 3: Generate session
        session_data = self.kite.generate_session(request_token, api_secret=KITE_API_SECRET)
        access_token = session_data["access_token"]
        self.kite.set_access_token(access_token)

        # Store in DB
        save_session(access_token)
        logger.info("Kite login successful")
        return access_token

    def get_valid_token(self) -> str:
        """Return cached token if valid, else re-login."""
        stored = get_session()
        if stored:
            self.kite.set_access_token(stored["access_token"])
            if self.is_token_valid():
                return stored["access_token"]

        # Retry login up to 3 times
        for attempt in range(3):
            try:
                return self.login()
            except Exception as e:
                logger.error(f"Login attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    time.sleep(30)
        raise RuntimeError("All login attempts failed")

    def is_token_valid(self) -> bool:
        """Check if the current access token works."""
        try:
            self.kite.profile()
            return True
        except Exception:
            return False

    def get_kite(self) -> KiteConnect:
        """Return authenticated KiteConnect instance."""
        self.get_valid_token()
        return self.kite
