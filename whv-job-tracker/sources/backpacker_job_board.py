import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

BASE_URL   = "https://www.backpackerjobboard.com.au"
SEARCH_URL = BASE_URL + "/search/{slug}/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}


def _city_slug(city: str) -> str:
    return city.lower().replace(" ", "-")


def _job_id(url: str) -> str:
    return "bjb_" + hashlib.md5(url.encode()).hexdigest()


def _parse_posted(text: str) -> str | None:
    m = re.match(r"(\d+)d ago", text.strip())
    if m:
        dt = datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
        return dt.date().isoformat()
    return None


def _scrape_page(html: str, city: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    now  = datetime.now(timezone.utc).isoformat()
    jobs = []

    for entry in soup.select("div.job-entry"):
        meta = entry.select_one("div.job-meta")
        if not meta:
            continue

        title_el = meta.select_one("h3.job-title a")
        if not title_el:
            continue

        title = title_el.get_text(strip=True)
        url   = title_el.get("href") or ""
        if url and not url.startswith("http"):
            url = BASE_URL + url

        company_el = meta.select_one("p.job-company")
        company    = " ".join(company_el.get_text().split()) if company_el else None

        location_el = meta.select_one("p.job-location")
        location    = " ".join(location_el.get_text().split()) if location_el else city

        # First p.job-published found in entry (the mobile-only sidebar one)
        pub_el    = entry.select_one("p.job-published")
        posted_at = _parse_posted(pub_el.get_text()) if pub_el else None

        jobs.append({
            "id":          _job_id(url),
            "source":      "backpacker_job_board",
            "title":       title,
            "company":     company or None,
            "location":    location,
            "city":        city,
            "description": None,
            "url":         url or None,
            "salary_min":  None,
            "salary_max":  None,
            "posted_at":   posted_at,
            "fetched_at":  now,
        })

    return jobs


def _max_page(html: str) -> int:
    soup     = BeautifulSoup(html, "html.parser")
    paginate = soup.select_one("ul.paginate")
    if not paginate:
        return 1
    nums = [
        int(a.get_text(strip=True))
        for a in paginate.select("a")
        if a.get_text(strip=True).isdigit()
    ]
    return max(nums, default=1)


def fetch(config: dict) -> list[dict]:
    cities      = config["search"]["cities"]
    max_results = config.get("backpacker_job_board", {}).get("max_results", 100)

    session = requests.Session()
    session.headers.update(_HEADERS)

    results: dict[str, dict] = {}

    for city in cities:
        if len(results) >= max_results:
            break

        slug     = _city_slug(city)
        max_pg   = None   # discovered after first page fetch
        page     = 1

        while len(results) < max_results:
            url = SEARCH_URL.format(slug=slug)
            if page > 1:
                url += f"?p={page}"

            try:
                resp = session.get(url, timeout=15)
                resp.raise_for_status()
            except Exception as exc:
                print(
                    f"[backpacker_job_board] request error ({city!r}, p{page}): {exc}",
                    file=sys.stderr,
                )
                break

            if max_pg is None:
                max_pg = _max_page(resp.text)

            jobs = _scrape_page(resp.text, city)
            if not jobs:
                break

            for j in jobs:
                results[j["id"]] = j

            if page >= max_pg:
                break
            page += 1

    total = len(results)
    print(f"[backpacker_job_board] fetched {total} unique jobs", file=sys.stderr)
    return list(results.values())[:max_results]
