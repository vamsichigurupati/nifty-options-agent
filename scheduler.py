import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from config.settings import (
    SCAN_INTERVAL_SECONDS, PRE_MARKET_LOGIN, SCAN_START, SCAN_END,
    DAILY_SUMMARY_TIME, SEND_DAILY_SUMMARY
)

logger = logging.getLogger(__name__)


class TradingScheduler:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    def schedule_pre_market(self, func):
        """Schedule pre-market login at 9:00 AM IST."""
        h, m = PRE_MARKET_LOGIN.split(":")
        self.scheduler.add_job(
            func, CronTrigger(hour=int(h), minute=int(m), day_of_week="mon-fri"),
            id="pre_market", replace_existing=True,
        )
        logger.info(f"Pre-market login scheduled at {PRE_MARKET_LOGIN}")

    def schedule_scanner(self, func):
        """Schedule options scanner every SCAN_INTERVAL_SECONDS during market hours."""
        self.scheduler.add_job(
            func, "interval", seconds=SCAN_INTERVAL_SECONDS,
            id="scanner", replace_existing=True,
        )
        logger.info(f"Scanner scheduled every {SCAN_INTERVAL_SECONDS}s")

    def schedule_eod(self, func):
        """Schedule end-of-day squareoff and summary."""
        h, m = SCAN_END.split(":")
        self.scheduler.add_job(
            func, CronTrigger(hour=int(h), minute=int(m), day_of_week="mon-fri"),
            id="eod_squareoff", replace_existing=True,
        )
        logger.info(f"EOD squareoff scheduled at {SCAN_END}")

    def schedule_daily_summary(self, func):
        """Schedule daily P&L summary."""
        if not SEND_DAILY_SUMMARY:
            return
        h, m = DAILY_SUMMARY_TIME.split(":")
        self.scheduler.add_job(
            func, CronTrigger(hour=int(h), minute=int(m), day_of_week="mon-fri"),
            id="daily_summary", replace_existing=True,
        )
        logger.info(f"Daily summary scheduled at {DAILY_SUMMARY_TIME}")

    def schedule_exit_check(self, func, interval_seconds=5):
        """Schedule periodic exit condition checks."""
        self.scheduler.add_job(
            func, "interval", seconds=interval_seconds,
            id="exit_check", replace_existing=True,
        )

    def start(self):
        self.scheduler.start()
        logger.info("Scheduler started")

    def stop(self):
        self.scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")

    def is_market_hours(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5:  # Saturday, Sunday
            return False
        start = datetime.strptime(SCAN_START, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
        end = datetime.strptime(SCAN_END, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
        return start <= now <= end
