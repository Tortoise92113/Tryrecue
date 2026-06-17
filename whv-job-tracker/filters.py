import re

_TITLE_BLOCK = re.compile(
    r'\bsenior\b'
    r'|\bchef\b'
    r'|\bfarmer\b|\bfarming\b|\bfarm\s+manager\b|\bfarm\s+hand\b'
    r'|\bfarm\s+worker\b|\bfarm\s+assistant\b|\bagricultural\b|\bagriculture\b',
    re.IGNORECASE,
)
_LOCATION_BLOCK = re.compile(r'\bsydney\b', re.IGNORECASE)


def pre_filter(jobs: list[dict]) -> tuple[list[dict], int]:
    """Filter out jobs by title or location. Returns (kept, dropped_count)."""
    kept, dropped = [], 0
    for job in jobs:
        title    = job.get("title", "")
        location = job.get("location") or job.get("city") or ""
        if _TITLE_BLOCK.search(title) or _LOCATION_BLOCK.search(location):
            dropped += 1
        else:
            kept.append(job)
    return kept, dropped
