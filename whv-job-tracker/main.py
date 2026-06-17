"""
main.py — single run of the full pipeline:
  1. Fetch jobs  (Adzuna → Backpacker Job Board → Jora)
  2. Classify new jobs with Gemini
  3. Send daily digest email
"""

import logging
import re

from config import load_config

import storage
from analyze import run_analysis
from classifier import GeminiNetworkError, classify_batch
from notifier import send_update_notification
from sources import adzuna, backpacker_job_board, jora

_log = logging.getLogger("main")

_TITLE_BLOCK = re.compile(
    r'\bsenior\b'
    r'|\bchef\b'
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
        _log.info("pre-filter dropped %d jobs (title/location filter)", dropped)
    return kept


def run(config: dict | None = None):
    if config is None:
        config = load_config()

    storage.init_db()

    # ── 1. Fetch ───────────────────────────────────────────────────────────
    all_jobs: list[dict] = []
    source_errors: list[str] = []

    _log.info("fetching Adzuna…")
    try:
        all_jobs += adzuna.fetch(config)
    except Exception as exc:
        _log.warning("Adzuna fetch failed: %s", exc)

    if config.get("backpacker_job_board", {}).get("enabled", True):
        _log.info("fetching Backpacker Job Board…")
        try:
            all_jobs += backpacker_job_board.fetch(config)
        except Exception as exc:
            _log.warning("Backpacker Job Board fetch failed: %s", exc)
            source_errors.append(f"⚠️ Backpacker Job Board 今日不可用（原因：{type(exc).__name__}）")
    else:
        _log.info("Backpacker Job Board disabled in config — skipping")

    if config.get("jora_scraping", False):
        _log.info("fetching Jora…")
        try:
            all_jobs += jora.fetch(config)
        except Exception as exc:
            _log.warning("Jora fetch failed: %s", exc)

    all_jobs = _pre_filter(all_jobs)

    if not all_jobs:
        _log.warning("no jobs fetched from any source — exiting")
        return

    # ── 2. Store & find unclassified ──────────────────────────────────────
    inserted = storage.upsert_jobs(all_jobs)
    _log.info("inserted %d new jobs (%d total fetched)", inserted, len(all_jobs))

    unclassified_ids = storage.get_unclassified_job_ids()
    jobs_to_classify = [j for j in all_jobs if j["id"] in unclassified_ids]
    _log.info(
        "%d to classify, %d already classified (skipped)",
        len(jobs_to_classify),
        len(all_jobs) - len(jobs_to_classify),
    )

    if jobs_to_classify:
        try:
            classify_batch(
                config,
                jobs_to_classify,
                on_result=lambda job_id, result: storage.update_classifier(job_id, result),
            )
        except GeminiNetworkError as exc:
            _log.error("classification skipped — network unreachable: %s", exc)
            source_errors.append("⚠️ Gemini 分類失敗（網路無法連線），職缺已儲存，下次執行補分類")

    deleted = storage.soft_delete_non_whv_jobs()
    if deleted:
        _log.info("soft-deleted %d non-WHV-friendly jobs", deleted)

    # ── 3. Analyse & update analytics.db ─────────────────────────────────
    _log.info("running analysis snapshot…")
    try:
        run_analysis()
    except Exception as exc:
        _log.warning("analysis failed: %s", exc)

    # ── 4. Notify ─────────────────────────────────────────────────────────
    _log.info("sending update notification…")
    try:
        send_update_notification(config, source_errors=source_errors or None)
    except Exception as exc:
        _log.warning("email failed: %s", exc)

    _log.info("pipeline complete")


def classify_only(config: dict | None = None):
    """Classify all unclassified jobs already in DB, without fetching new ones."""
    if config is None:
        config = load_config()
    storage.init_db()
    jobs = storage.get_unclassified_jobs()
    _log.info("%d unclassified jobs to classify", len(jobs))
    if not jobs:
        _log.info("nothing to classify")
        return
    classify_batch(
        config,
        jobs,
        on_result=lambda job_id, result: storage.update_classifier(job_id, result),
    )
    deleted = storage.soft_delete_non_whv_jobs()
    if deleted:
        _log.info("soft-deleted %d non-WHV-friendly jobs", deleted)

    _log.info("running analysis snapshot…")
    try:
        run_analysis()
    except Exception as exc:
        _log.warning("analysis failed: %s", exc)

    _log.info("sending update notification…")
    try:
        send_update_notification(config)
    except Exception as exc:
        _log.warning("email failed: %s", exc)

    _log.info("classify_only complete")


if __name__ == "__main__":
    run()
