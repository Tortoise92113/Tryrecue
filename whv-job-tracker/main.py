"""
main.py — single run of the full pipeline (this branch does not send email):
  1. Fetch jobs  (Adzuna → Backpacker Job Board → Jora)
  2. Classify new jobs with Gemini
  3. Publish the GitHub Pages report
"""

import logging
import sys

from config import load_config

import storage
from analyze import run_analysis
from classifier import GeminiNetworkError, classify_batch
from filters import pre_filter
from sources import adzuna, backpacker_job_board, jora

_log = logging.getLogger("main")


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

    all_jobs, dropped = pre_filter(all_jobs, config)
    if dropped:
        _log.info("pre-filter dropped %d jobs (title/location filter)", dropped)

    if not all_jobs:
        _log.warning("no jobs fetched from any source — exiting")
        return

    # ── 2. Store & find unclassified ──────────────────────────────────────
    inserted = storage.upsert_jobs(all_jobs)
    _log.info("inserted %d new jobs (%d total fetched)", inserted, len(all_jobs))

    storage.backfill_jora_city()

    presence = storage.update_job_presence({j["id"] for j in all_jobs})
    _log.info(
        "=== job presence — revived: %d | newly delisted: %d ===",
        presence["revived"], presence["newly_delisted"],
    )

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

    # ── 4. Publish GitHub Pages report ────────────────────────────────────
    _log.info("publishing GitHub Pages report…")
    try:
        from pages_publisher import publish_today
        result = publish_today()
        _log.info(
            "pages: today=%d %s | all=%d %s  (push_ok=%s)",
            result.today_count, result.today_url,
            result.all_count, result.all_url, result.push_ok,
        )
        if not result.push_ok:
            _log.warning("git push failed — Pages link may not be updated yet")
    except Exception as exc:
        _log.warning("pages publish failed (non-fatal): %s", exc)

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
    try:
        classify_batch(
            config,
            jobs,
            on_result=lambda job_id, result: storage.update_classifier(job_id, result),
        )
    except GeminiNetworkError as exc:
        _log.error("classification skipped — network unreachable: %s", exc)
        sys.exit(1)
    except Exception as exc:
        _log.error("unexpected classification error: %s", exc, exc_info=True)
        sys.exit(1)
    deleted = storage.soft_delete_non_whv_jobs()
    if deleted:
        _log.info("soft-deleted %d non-WHV-friendly jobs", deleted)

    _log.info("running analysis snapshot…")
    try:
        run_analysis()
    except Exception as exc:
        _log.warning("analysis failed: %s", exc)

    _log.info("classify_only complete")


if __name__ == "__main__":
    run()
