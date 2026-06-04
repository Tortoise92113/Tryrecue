"""
main.py — single run of the full pipeline:
  1. Fetch jobs  (Adzuna → Backpacker Job Board → Jora)
  2. Classify new jobs with Gemini
  3. Send daily digest email
"""

import re
import sys
from pathlib import Path

from config import load_config

import storage
from analyze import run_analysis
from classifier import classify_batch
from notifier import send_update_notification
from sources import adzuna, backpacker_job_board, jora

_TITLE_BLOCK = re.compile(
    r'\bsenior\b'
    r'|\bfarmer\b|\bfarming\b|\bfarm\s+manager\b|\bfarm\s+hand\b'
    r'|\bfarm\s+worker\b|\bfarm\s+assistant\b|\bagricultural\b|\bagriculture\b',
    re.IGNORECASE,
)
_LOCATION_BLOCK = re.compile(r'\bsydney\b', re.IGNORECASE)


def _pre_filter(jobs: list[dict]) -> list[dict]:
    kept, dropped = [], 0
    for job in jobs:
        title    = job.get("title", "")
        location = job.get("location") or job.get("city") or ""
        if _TITLE_BLOCK.search(title) or _LOCATION_BLOCK.search(location):
            dropped += 1
        else:
            kept.append(job)
    if dropped:
        print(f"[main] pre-filter dropped {dropped} jobs (title/location filter)", file=sys.stderr)
    return kept


def run(config: dict | None = None):
    if config is None:
        config = load_config()

    storage.init_db()

    # ── 1. Fetch ───────────────────────────────────────────────────────────
    all_jobs: list[dict] = []
    source_errors: list[str] = []

    print("[main] fetching Adzuna…", file=sys.stderr)
    try:
        all_jobs += adzuna.fetch(config)
    except Exception as exc:
        print(f"[main] Adzuna fetch failed: {exc}", file=sys.stderr)

    if config.get("backpacker_job_board", {}).get("enabled", True):
        print("[main] fetching Backpacker Job Board…", file=sys.stderr)
        try:
            all_jobs += backpacker_job_board.fetch(config)
        except Exception as exc:
            print(f"[main] Backpacker Job Board fetch failed: {exc}", file=sys.stderr)
            source_errors.append(f"⚠️ Backpacker Job Board 今日不可用（原因：{type(exc).__name__}）")
    else:
        print("[main] Backpacker Job Board disabled in config — skipping", file=sys.stderr)

    if config.get("jora_scraping", False):
        print("[main] fetching Jora…", file=sys.stderr)
        try:
            all_jobs += jora.fetch(config)
        except Exception as exc:
            print(f"[main] Jora fetch failed: {exc}", file=sys.stderr)

    all_jobs = _pre_filter(all_jobs)

    if not all_jobs:
        print("[main] no jobs fetched from any source — exiting", file=sys.stderr)
        return

    # ── 2. Store & find unclassified ──────────────────────────────────────
    inserted = storage.upsert_jobs(all_jobs)
    print(f"[main] inserted {inserted} new jobs ({len(all_jobs)} total fetched)", file=sys.stderr)

    # Only classify jobs that don't have a result yet
    with storage.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, company, location, city, description "
            "FROM jobs WHERE is_whv_friendly IS NULL"
        ).fetchall()
    unclassified = [dict(r) for r in rows]
    print(f"[main] {len(unclassified)} jobs need classification", file=sys.stderr)

    if unclassified:
        classify_batch(
            config,
            unclassified,
            on_result=lambda job_id, result: storage.update_classifier(job_id, result),
        )

    with storage.get_conn() as conn:
        deleted = conn.execute("DELETE FROM jobs WHERE is_whv_friendly = 0").rowcount
    if deleted:
        print(f"[main] removed {deleted} non-WHV-friendly jobs after classification", file=sys.stderr)

    # ── 3. Analyse & update analytics.db ─────────────────────────────────
    print("[main] running analysis snapshot…", file=sys.stderr)
    try:
        run_analysis()
    except Exception as exc:
        print(f"[main] analysis failed: {exc}", file=sys.stderr)

    # ── 4. Notify ─────────────────────────────────────────────────────────
    print("[main] sending update notification…", file=sys.stderr)
    try:
        send_update_notification(config, source_errors=source_errors or None)
    except Exception as exc:
        print(f"[main] email failed: {exc}", file=sys.stderr)

    print("[main] pipeline complete", file=sys.stderr)


if __name__ == "__main__":
    run()
