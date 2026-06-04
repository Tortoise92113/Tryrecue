_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def create_browser(pw):
    """Launch a headless Chromium browser for au.jora.com / workforceaustralia.gov.au.
    Returns (browser, page). Caller is responsible for browser.close()."""
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(user_agent=_USER_AGENT, locale="en-AU")
    page = context.new_page()
    page.set_default_timeout(20_000)
    return browser, page


def el_text(card, sel: str) -> str:
    el = card.query_selector(sel)
    return el.inner_text().strip() if el else ""


def el_href(card, sel: str) -> str:
    el = card.query_selector(sel)
    return el.get_attribute("href") or "" if el else ""
