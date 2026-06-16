"""
run_scheduler.py — keeps the pipeline running on a daily schedule.

Usage:
    python run_scheduler.py            # runs scheduler + Flask dashboard
    python run_scheduler.py --no-web   # scheduler only (no Flask)

The scheduler fires main.run() once at startup (so you get data immediately)
and then daily at the time set in config.yml → schedule.run_time.
"""

import argparse
import logging as _logging
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import load_config

_log = _logging.getLogger("scheduler")


class _WeeklyFileHandler(_logging.FileHandler):
    """FileHandler that rotates to a new file every Monday.

    Filename format: W23 2026-0601_0607 main.log
    """

    def __init__(self, log_dir: Path):
        self._log_dir = log_dir
        self._week = date.today().isocalendar()[1]
        super().__init__(self._log_path(), encoding="utf-8")

    def _log_path(self) -> Path:
        today = date.today()
        mon = today - timedelta(days=today.weekday())
        sun = mon + timedelta(days=6)
        wk  = today.isocalendar()[1]
        return self._log_dir / f"W{wk:02d} {mon.year}-{mon:%m%d}_{sun:%m%d} main.log"

    def emit(self, record):
        wk = date.today().isocalendar()[1]
        if wk != self._week:
            self.acquire()
            try:
                if wk != self._week:
                    self._week = wk
                    self.close()
                    self.baseFilename = str(self._log_path())
                    self.stream = self._open()
            finally:
                self.release()
        super().emit(record)


def _setup_logging():
    """Route all log output (scheduler + main + classifier) to a weekly log file and stderr."""
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    fmt = _logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = _logging.getLogger()
    root.setLevel(_logging.INFO)
    fh = _WeeklyFileHandler(log_dir)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = _logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    _logging.getLogger("werkzeug").setLevel(_logging.WARNING)


def _run_pipeline():
    import main as pipeline
    from classifier import GeminiNetworkError
    try:
        config = load_config()
        pipeline.run(config)
    except GeminiNetworkError as exc:
        _log.error("%s", exc)
    except Exception as exc:
        _log.error("pipeline error: %s", exc)


def start_scheduler(config: dict) -> BackgroundScheduler:
    tz_name  = config.get("schedule", {}).get("timezone", "Australia/Darwin")
    run_time = config.get("schedule", {}).get("run_time", "08:00")
    hour, minute = map(int, run_time.split(":"))
    tz = pytz.timezone(tz_name)

    scheduler = BackgroundScheduler(timezone=tz)
    scheduler.add_job(
        _run_pipeline,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
        id="daily_pipeline",
        name=f"Daily pipeline @ {run_time} {tz_name}",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    _log.info("scheduled daily at %s %s", run_time, tz_name)
    return scheduler


def start_web(port: int = 5000):
    """Run Flask in a daemon thread so it doesn't block the scheduler."""
    import os
    sys.path.insert(0, str(Path(__file__).parent))
    os.environ.setdefault("FLASK_ENV", "production")

    from web.app import app
    import storage
    storage.init_db()

    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
        name="flask",
    )
    thread.start()
    _log.info("Flask dashboard running at http://localhost:%d", port)


def main():
    _setup_logging()

    parser = argparse.ArgumentParser(description="WHV Job Tracker scheduler")
    parser.add_argument("--no-web",   action="store_true", help="Disable Flask dashboard")
    parser.add_argument("--port",     type=int, default=5000, help="Flask port (default 5000)")
    parser.add_argument("--no-init",  action="store_true", help="Skip immediate pipeline run on startup")
    parser.add_argument("--run-once",      action="store_true", help="Run pipeline once with logging, then exit")
    parser.add_argument("--classify-only", action="store_true", help="Classify unclassified DB jobs without fetching")
    args = parser.parse_args()

    if args.run_once or args.classify_only:
        if args.classify_only:
            _log.info("=== classify-only run ===")
            import main as pipeline
            pipeline.classify_only()
        else:
            _log.info("=== manual run ===")
            _run_pipeline()
        _log.info("=== done ===")
        return

    config = load_config()

    if not args.no_web:
        start_web(port=args.port)

    scheduler = start_scheduler(config)

    if not args.no_init:
        _log.info("running pipeline once now…")
        t = threading.Thread(target=_run_pipeline, daemon=True, name="init_pipeline")
        t.start()

    _log.info("press Ctrl+C to stop")
    try:
        import time
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        _log.info("shutting down…")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
