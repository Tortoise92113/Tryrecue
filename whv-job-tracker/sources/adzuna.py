import hashlib
import logging
import re
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.adzuna.com/v1/api/jobs/au/search/{page}"

_logger = logging.getLogger("adzuna")

_APP_KEY_RE = re.compile(r'(app_key=)[^&\s]+')


def _mask_key(text: str) -> str:
    """Replace app_key value in URLs with *** to prevent API key leakage in logs."""
    return _APP_KEY_RE.sub(r'\1***', text)


def _job_id(raw: dict) -> str:
    return "adzuna_" + raw.get("id", hashlib.md5(raw.get("redirect_url", "").encode()).hexdigest())


def _parse(raw: dict, city: str) -> dict:
    salary = raw.get("salary_min"), raw.get("salary_max")
    return {
        "id":          _job_id(raw),
        "source":      "adzuna",
        "title":       raw.get("title", "").strip(),
        "company":     (raw.get("company") or {}).get("display_name"),
        "location":    (raw.get("location") or {}).get("display_name"),
        "city":        city,
        "description": raw.get("description", "").strip(),
        "url":         raw.get("redirect_url"),
        "salary_min":  salary[0],
        "salary_max":  salary[1],
        "posted_at":   raw.get("created"),
        "fetched_at":  datetime.now(timezone.utc).isoformat(),
    }


def _fetch_with_retry(url: str, params: dict, max_retries: int = 3, base_delay: float = 2.0):
    last_exc = None
    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 503:
                last_exc = exc
                if attempt < max_retries - 1:
                    time.sleep(base_delay * (attempt + 1))
                    continue
            raise
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(base_delay * (attempt + 1))
                continue
            raise
    raise last_exc


def fetch(config: dict) -> list[dict]:
    app_id  = config["adzuna"]["app_id"]
    app_key = config["adzuna"]["app_key"]
    cities  = config["search"]["cities"]
    keywords = config["search"]["keywords"]
    max_per_query = config["search"].get("max_results_per_query", 50)
    page_size = min(50, max_per_query)

    results: dict[str, dict] = {}
    total_queries = 0
    failed_queries: list[tuple] = []

    for city in cities:
        for keyword in keywords:
            # Adzuna searches job titles — strip meta-phrases that appear in config keywords
            keyword = re.sub(r'\s+working\s+holiday', '', keyword, flags=re.IGNORECASE).strip()
            total_queries += 1
            pages_needed = -(-max_per_query // page_size)  # ceiling division
            query_failed = False
            for page in range(1, pages_needed + 1):
                params = {
                    "app_id":           app_id,
                    "app_key":          app_key,
                    "results_per_page": page_size,
                    "what":             keyword,
                    "where":            city,
                }
                try:
                    resp = _fetch_with_retry(BASE_URL.format(page=page), params)
                    data = resp.json()
                except requests.RequestException as exc:
                    _logger.warning("[adzuna] request error (%r, %r, p%d): %s", city, keyword, page, _mask_key(str(exc)))
                    query_failed = True
                    break

                jobs = data.get("results", [])
                if not jobs:
                    break

                for raw in jobs:
                    job = _parse(raw, city)
                    results[job["id"]] = job  # deduplicate by id

                if len(jobs) < page_size:
                    break

                time.sleep(0.3)  # be polite to the API

            if query_failed:
                failed_queries.append((city, keyword))

    if failed_queries:
        _logger.warning(
            "=== Adzuna fetch complete — %d queries attempted | %d failed (after retry): %s ===",
            total_queries, len(failed_queries), failed_queries,
        )
    else:
        _logger.info(
            "=== Adzuna fetch complete — %d queries attempted | 0 failed ===",
            total_queries,
        )

    _logger.info("[adzuna] fetched %d unique jobs", len(results))
    return list(results.values())
