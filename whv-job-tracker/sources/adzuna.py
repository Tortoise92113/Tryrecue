import hashlib
import re
import sys
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.adzuna.com/v1/api/jobs/au/search/{page}"


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


def fetch(config: dict) -> list[dict]:
    app_id  = config["adzuna"]["app_id"]
    app_key = config["adzuna"]["app_key"]
    cities  = config["search"]["cities"]
    keywords = config["search"]["keywords"]
    max_per_query = config["search"].get("max_results_per_query", 50)
    page_size = min(50, max_per_query)

    results: dict[str, dict] = {}

    for city in cities:
        for keyword in keywords:
            # Adzuna searches job titles — strip meta-phrases that appear in config keywords
            keyword = re.sub(r'\s+working\s+holiday', '', keyword, flags=re.IGNORECASE).strip()
            pages_needed = -(-max_per_query // page_size)  # ceiling division
            for page in range(1, pages_needed + 1):
                params = {
                    "app_id":           app_id,
                    "app_key":          app_key,
                    "results_per_page": page_size,
                    "what":             keyword,
                    "where":            city,
                }
                try:
                    resp = requests.get(
                        BASE_URL.format(page=page),
                        params=params,
                        timeout=15,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except requests.RequestException as exc:
                    print(f"[adzuna] request error ({city!r}, {keyword!r}, p{page}): {exc}", file=sys.stderr)
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

    print(f"[adzuna] fetched {len(results)} unique jobs", file=sys.stderr)
    return list(results.values())
