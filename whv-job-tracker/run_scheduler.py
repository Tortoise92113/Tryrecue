"""
run_scheduler.py — keeps the pipeline running on a daily schedule.

Usage:
    python run_scheduler.py            # runs scheduler + Flask dashboard
    python run_scheduler.py --no-web   # scheduler only (no Flask)

The scheduler fires main.run() once at startup (so you get data immediately)
and then daily at the time set in config.yml → schedule.run_time.
"""

import argparse
import sys
import threading
from pathlib import Path

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import main as pipeline
from config import load_config


def _run_pipeline():
    try:
        config = load_config()
        pipeline.run(config)
    except Exception as exc:
        print(f"[scheduler] pipeline error: {exc}", file=sys.stderr)


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
        misfire_grace_time=3600,  # allow up to 1-hour late fire (e.g. after sleep/resume)
    )
    scheduler.start()
    print(f"[scheduler] scheduled daily at {run_time} {tz_name}", file=sys.stderr)
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
    print(f"[scheduler] Flask dashboard running at http://localhost:{port}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="WHV Job Tracker scheduler")
    parser.add_argument("--no-web",  action="store_true", help="Disable Flask dashboard")
    parser.add_argument("--port",    type=int, default=5000, help="Flask port (default 5000)")
    parser.add_argument("--no-init", action="store_true", help="Skip immediate pipeline run on startup")
    args = parser.parse_args()

    config = load_config()

    if not args.no_web:
        start_web(port=args.port)

    scheduler = start_scheduler(config)

    if not args.no_init:
        print("[scheduler] running pipeline once now…", file=sys.stderr)
        # Run in a thread so startup logs are cleaner
        t = threading.Thread(target=_run_pipeline, daemon=True, name="init_pipeline")
        t.start()

    print("[scheduler] press Ctrl+C to stop", file=sys.stderr)
    try:
        # Keep the main thread alive
        import time
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        print("\n[scheduler] shutting down…", file=sys.stderr)
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
