import asyncio
import json
import logging
import os
import random

from google import genai

MAX_RETRIES = 5
SEMAPHORE_LIMIT = int(os.getenv("GEMINI_CONCURRENCY", "8"))
_RAMP_INITIAL = 3     # concurrent requests at cold start
_RAMP_SECONDS = 15.0  # seconds to ramp from _RAMP_INITIAL to SEMAPHORE_LIMIT

_DESC_TRUNCATE_LEN = 1500  # max description chars sent to Gemini
_NOTE_MAX_LEN = 200        # max classifier_note chars stored in DB

_logger = logging.getLogger("classifier")
_logger.setLevel(logging.INFO)

# Gemini 2.5 Flash (non-thinking) pricing
_PRICE_INPUT_PER_TOKEN  = 0.15 / 1_000_000   # $0.15 per 1M input tokens
_PRICE_OUTPUT_PER_TOKEN = 0.60 / 1_000_000   # $0.60 per 1M output tokens

_FAILURE_RESULT = {
    "is_whv_friendly":  -1,
    "visa_sponsorship": 0,
    "accommodation":    0,
    "urgency":          "normal",
    "classifier_note":  "classification failed after retries",
    "job_type":         "other",
    "regional_area":    None,
}

PROMPT_TEMPLATE = """You are a job classifier for Australian Working Holiday Visa (WHV/subclass 417 & 462) holders.

Analyse this job listing and return ONLY a valid JSON object with these exact keys:
- "is_whv_friendly": 1 if the job is suitable for WHV holders (short-term, casual, seasonal, backpacker-friendly), else 0
- "visa_sponsorship": 1 if the listing mentions visa sponsorship or employer-sponsored visa, else 0
- "accommodation": 1 if the listing mentions accommodation provided, else 0
- "urgency": one of "high" (hiring immediately / ASAP), "normal", or "low" (no urgency signal)
- "note": one short sentence (max 20 words) explaining your is_whv_friendly decision
- "job_type": one of "hospitality", "hotel", "retail", "farm", "office", "other"
- "regional_area": true if the job location is in an Australian WHV regional area (e.g. Cairns, Darwin, Townsville, Broome, Alice Springs, any rural/regional area outside major cities like Sydney/Melbourne/Brisbane/Perth/Adelaide); false if it is in a major metropolitan city; null if cannot be determined

Australian WHV regional areas include all of regional and rural Australia. Major cities that are NOT regional: Sydney, Melbourne, Brisbane, Perth, Adelaide, Canberra, Gold Coast.
Cities that ARE regional/WHV-eligible for second/third year visa: Cairns, Darwin, Townsville, Broome, Alice Springs, Hobart, Launceston, Rockhampton, Mackay, Bundaberg, Geraldton, Port Hedland, Katherine, and all rural/outback areas.

If the job URL contains "backpackerjobboard.com.au", assume is_whv_friendly = 1 unless the title or description explicitly states otherwise.

Job title: {title}
Company: {company}
Location: {location}
URL: {url}
Description:
{description}

Respond with ONLY the JSON object, no markdown, no explanation."""


_VALID_JOB_TYPES = ("hospitality", "hotel", "retail", "farm", "office", "other")


def _validate(result: dict) -> dict:
    raw_regional = result.get("regional_area")
    if raw_regional is True or raw_regional == 1:
        regional_area = 1
    elif raw_regional is False or raw_regional == 0:
        regional_area = 0
    else:
        regional_area = None

    job_type = result.get("job_type", "other")
    if job_type not in _VALID_JOB_TYPES:
        job_type = "other"

    return {
        "is_whv_friendly":  int(bool(result.get("is_whv_friendly"))),
        "visa_sponsorship": int(bool(result.get("visa_sponsorship"))),
        "accommodation":    int(bool(result.get("accommodation"))),
        "urgency":          result.get("urgency", "normal") if result.get("urgency") in ("high", "normal", "low") else "normal",
        "classifier_note":  str(result.get("note") or "")[:_NOTE_MAX_LEN],
        "job_type":         job_type,
        "regional_area":    regional_area,
    }


def _is_503(exc: Exception) -> bool:
    s = str(exc)
    return (
        "503" in s
        or "UNAVAILABLE" in s
        or "high demand" in s.lower()
        or "overloaded" in s.lower()
    )


def _backoff_time(attempt: int, base: float = 2.0, max_wait: float = 60.0) -> float:
    """Exponential backoff with ±30% jitter. attempt starts at 0."""
    wait = base ** attempt
    jitter = wait * 0.3 * random.uniform(-1, 1)
    return min(wait + jitter, max_wait)


def _strip_markdown_fence(text: str) -> str:
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text


async def _ramp_up(semaphore: asyncio.Semaphore, initial: int, target: int, ramp_seconds: float) -> None:
    """Gradually release semaphore slots from initial to target over ramp_seconds."""
    steps = target - initial
    if steps <= 0:
        return
    delay = ramp_seconds / steps
    for _ in range(steps):
        await asyncio.sleep(delay)
        semaphore.release()


async def _process_job(client, model_name: str, semaphore: asyncio.Semaphore,
                       job: dict, on_result, token_counts: list,
                       api_call_counter: list, err_stats: dict) -> bool:
    prompt = PROMPT_TEMPLATE.format(
        title=job.get("title", ""),
        company=job.get("company") or "Unknown",
        location=job.get("location") or job.get("city") or "Unknown",
        url=job.get("url") or "",
        description=(job.get("description") or "")[:_DESC_TRUNCATE_LEN],
    )

    had_retry = False

    for attempt in range(MAX_RETRIES):
        if attempt > 0:
            had_retry = True

        backoff = 0.0
        async with semaphore:
            api_call_counter[0] += 1
            _logger.info("API call #%d — job %r (attempt %d/%d)",
                         api_call_counter[0], job.get("id"), attempt + 1, MAX_RETRIES)
            try:
                response = await client.aio.models.generate_content(
                    model=model_name, contents=prompt
                )
                usage = getattr(response, "usage_metadata", None)
                if usage:
                    token_counts.append((
                        getattr(usage, "prompt_token_count", 0) or 0,
                        getattr(usage, "candidates_token_count", 0) or 0,
                    ))
                text = _strip_markdown_fence(response.text.strip())
                result = _validate(json.loads(text.strip()))
                if on_result:
                    on_result(job["id"], result)
                if had_retry:
                    err_stats["retried_jobs"] += 1
                    err_stats["retried_success"] += 1
                return True
            except json.JSONDecodeError as exc:
                _logger.warning("JSON parse error (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, exc)
                backoff = _backoff_time(attempt, base=1.5, max_wait=10.0)
            except Exception as exc:
                if _is_503(exc):
                    backoff = _backoff_time(attempt)
                    err_stats["503_count"] += 1
                    err_stats["max_backoff"] = max(err_stats["max_backoff"], backoff)
                    _logger.warning("503 on job %r attempt %d/%d, backoff %.1fs",
                                    job.get("id"), attempt + 1, MAX_RETRIES, backoff)
                else:
                    _logger.warning("API error (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, exc)
                    break  # non-503: don't retry

        # Sleep outside the semaphore so the slot is free during the wait
        if backoff > 0:
            await asyncio.sleep(backoff)

    _logger.error("Gave up on job %r after %d attempts", job.get("id"), MAX_RETRIES)
    if had_retry:
        err_stats["retried_jobs"] += 1
    if on_result:
        on_result(job["id"], _FAILURE_RESULT)
    return False


async def _classify_all(config: dict, jobs: list[dict], on_result) -> tuple[int, int]:
    client = genai.Client(api_key=config["gemini"]["api_key"])
    model_name = config["gemini"]["model"]

    # Re-read env var here so callers can set it after module import
    semaphore_limit = int(os.getenv("GEMINI_CONCURRENCY", str(SEMAPHORE_LIMIT)))

    # Start with reduced concurrency and ramp up to avoid cold-start 503 burst
    semaphore = asyncio.Semaphore(_RAMP_INITIAL)
    ramp_task = asyncio.create_task(
        _ramp_up(semaphore, _RAMP_INITIAL, semaphore_limit, _RAMP_SECONDS)
    )

    token_counts: list = []
    api_call_counter: list[int] = [0]  # single-element list so coroutines can mutate it
    err_stats = {"503_count": 0, "max_backoff": 0.0, "retried_jobs": 0, "retried_success": 0}

    _logger.info("=== Classification batch start — %d jobs to process (concurrency: %d→%d over %.0fs) ===",
                 len(jobs), _RAMP_INITIAL, semaphore_limit, _RAMP_SECONDS)

    results = await asyncio.gather(
        *[_process_job(client, model_name, semaphore, job, on_result,
                       token_counts, api_call_counter, err_stats) for job in jobs]
    )
    ramp_task.cancel()
    try:
        await ramp_task
    except asyncio.CancelledError:
        pass

    classified = sum(results)
    skipped = len(results) - classified
    total_calls = api_call_counter[0]
    retries = total_calls - len(jobs)

    retried = err_stats["retried_jobs"]
    retry_success_rate = (
        f"{err_stats['retried_success'] / retried * 100:.0f}%" if retried else "n/a"
    )

    if token_counts:
        total_input  = sum(t[0] for t in token_counts)
        total_output = sum(t[1] for t in token_counts)
        cost = total_input * _PRICE_INPUT_PER_TOKEN + total_output * _PRICE_OUTPUT_PER_TOKEN
        token_suffix = f" | tokens in/out: {total_input:,}/{total_output:,} | cost: ${cost:.4f} USD"
    else:
        token_suffix = ""

    _logger.info(
        "=== Batch complete — jobs: %d | API calls: %d | retries: %d | "
        "503_errors: %d | max_backoff: %.1fs | retry_success_rate: %s | "
        "classified: %d | failed: %d%s ===",
        len(jobs), total_calls, retries,
        err_stats["503_count"], err_stats["max_backoff"], retry_success_rate,
        classified, skipped, token_suffix,
    )

    return classified, skipped


def classify_batch(config: dict, jobs: list[dict],
                   on_result=None) -> tuple[int, int]:
    return asyncio.run(_classify_all(config, jobs, on_result))
