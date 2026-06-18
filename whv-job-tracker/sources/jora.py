import hashlib
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

_SALARY_RE = re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*[-–]\s*\$\s*([\d,]+(?:\.\d+)?)')

# Playwright is an optional dependency — import lazily so the rest of the
# app continues to work even if it is not installed or the browser is missing.
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from sources.scraper_utils import create_browser, el_text, el_href
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

BASE_URL = "https://au.jora.com/jobs"


def _strip_tracking(url: str) -> str:
    """Remove Jora tracking query params — job identity lives in the path only."""
    if not url:
        return url
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def _job_id(url: str, title: str, company: str) -> str:
    key = _strip_tracking(url) if url else (title + (company or ""))
    return "jora_" + hashlib.md5(key.encode()).hexdigest()


def _scrape_page(page, city: str) -> list[dict]:
    jobs = []
    cards = page.query_selector_all("article.job-card, [data-testid='job-card'], .job-result")
    if not cards:
        # fallback: try generic result containers
        cards = page.query_selector_all("[class*='job'][class*='card'], [class*='result']")

    for card in cards:
        title   = (el_text(card, "h2 a, h3 a, [data-testid='job-title'], .job-title a")
                   or el_text(card, "h2, h3, .title")).strip()
        company = el_text(card, "span.job-company, [data-testid='company-name'], .company, .employer").strip()
        location= el_text(card, "a.job-location, [data-testid='job-location'], .location, .suburb").strip()
        snippet = el_text(card, ".job-abstract, .job-description, .teaser, .description").strip()
        raw_url = el_href(card, "h2 a, h3 a, [data-testid='job-title'], a.job-link")

        salary_min = salary_max = None
        for badge_el in card.query_selector_all(".badge .content"):
            m = _SALARY_RE.search(badge_el.inner_text())
            if m:
                salary_min = float(m.group(1).replace(",", ""))
                salary_max = float(m.group(2).replace(",", ""))
                break
        if raw_url and raw_url.startswith("/"):
            raw_url = "https://au.jora.com" + raw_url
        raw_url = _strip_tracking(raw_url)

        if not title:
            continue

        jobs.append({
            "id":          _job_id(raw_url, title, company),
            "source":      "jora",
            "title":       title,
            "company":     company or None,
            "location":    location or city,
            "city":        city,
            "description": snippet or None,
            "url":         raw_url or None,
            "salary_min":  salary_min,
            "salary_max":  salary_max,
            "posted_at":   None,
            "fetched_at":  datetime.now(timezone.utc).isoformat(),
        })
    return jobs


def fetch(config: dict) -> list[dict]:
    if not config.get("jora_scraping", False):
        print("[jora] scraping disabled in config — skipping", file=sys.stderr)
        return []

    if not _PLAYWRIGHT_AVAILABLE:
        print("[jora] playwright not installed — skipping", file=sys.stderr)
        return []

    cities       = config["search"]["cities"]
    keywords     = config["search"]["keywords"]
    max_per_query = config["search"].get("max_results_per_query", 50)

    results: dict[str, dict] = {}

    try:
        with sync_playwright() as pw:
            browser, page = create_browser(pw)

            for city in cities:
                for keyword in keywords:
                    fetched = 0
                    pg_num  = 1
                    while fetched < max_per_query:
                        params = f"?q={keyword.replace(' ', '+')}&l={city.replace(' ', '+')}&p={pg_num}"
                        url    = BASE_URL + params
                        try:
                            page.goto(url, wait_until="domcontentloaded")
                            page.wait_for_timeout(1500)  # let JS render
                        except PWTimeout:
                            print(f"[jora] timeout loading {url} — skipping page", file=sys.stderr)
                            break
                        except Exception as exc:
                            print(f"[jora] navigation error ({city!r}, {keyword!r}, p{pg_num}): {exc}", file=sys.stderr)
                            break

                        # Check for bot detection / captcha
                        if any(s in page.title().lower() for s in ("captcha", "access denied", "robot")):
                            print(f"[jora] bot detection triggered on page {pg_num} — stopping Jora", file=sys.stderr)
                            browser.close()
                            return list(results.values())

                        jobs = _scrape_page(page, city)
                        if not jobs:
                            break

                        for j in jobs:
                            results[j["id"]] = j
                        fetched += len(jobs)

                        # Check if a "next page" link exists
                        next_btn = page.query_selector("a[aria-label='Next'], a[data-testid='next-page'], .pagination-next a")
                        if not next_btn:
                            break

                        pg_num += 1
                        time.sleep(1.5)  # polite crawl delay

            browser.close()

    except Exception as exc:
        # Any top-level failure must NOT propagate — log and return partial results
        print(f"[jora] unexpected error: {exc} — returning {len(results)} partial results", file=sys.stderr)

    print(f"[jora] fetched {len(results)} unique jobs", file=sys.stderr)
    return list(results.values())
1