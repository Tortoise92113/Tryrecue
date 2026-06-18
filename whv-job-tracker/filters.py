import re

from config import load_config


def _keyword_to_pattern(keyword: str) -> str:
    """Turn a keyword/phrase into a whole-word regex.

    'senior'       -> \\bsenior\\b
    'farm manager' -> \\bfarm\\s+manager\\b   (any run of whitespace between words)
    """
    parts = [re.escape(p) for p in keyword.split()]
    return r"\b" + r"\s+".join(parts) + r"\b"


def compile_blocklist(keywords: list[str]) -> re.Pattern | None:
    """Compile a list of keywords into one case-insensitive regex.

    Returns None for an empty list so callers can treat it as 'blocks nothing'.
    """
    cleaned = [k.strip() for k in (keywords or []) if k and k.strip()]
    if not cleaned:
        return None
    return re.compile("|".join(_keyword_to_pattern(k) for k in cleaned), re.IGNORECASE)


def pre_filter(jobs: list[dict], config: dict | None = None) -> tuple[list[dict], int]:
    """Filter out jobs by title or location. Returns (kept, dropped_count).

    Blocked keywords are read from config.yml (single source of truth):
        filters:
          title_block:    [senior, chef, ...]
          location_block: []
    A missing or empty list means "block nothing" for that field.
    """
    if config is None:
        config = load_config()
    filters_cfg = config.get("filters", {}) or {}

    title_block = compile_blocklist(filters_cfg.get("title_block"))
    location_block = compile_blocklist(filters_cfg.get("location_block"))

    kept, dropped = [], 0
    for job in jobs:
        title = job.get("title", "")
        location = job.get("location") or job.get("city") or ""
        if (title_block and title_block.search(title)) or (
            location_block and location_block.search(location)
        ):
            dropped += 1
        else:
            kept.append(job)
    return kept, dropped
