import asyncio
import json
import logging
import os
import random
import socket
import sys
import threading

from google import genai

MAX_RETRIES = 5
SEMAPHORE_LIMIT = int(os.getenv("GEMINI_CONCURRENCY", "8"))
_RAMP_INITIAL = 1     # concurrent requests at cold start
_RAMP_SECONDS = 30.0  # seconds to ramp from _RAMP_INITIAL to SEMAPHORE_LIMIT
_GEMINI_API_HOST = "generativelanguage.googleapis.com"
_DNS_CHECK_TIMEOUT_SECONDS = float(os.getenv("GEMINI_DNS_CHECK_TIMEOUT", "5"))

_DESC_TRUNCATE_LEN = 800   # max description chars sent to Gemini
_NOTE_MAX_LEN = 80         # max classifier_note chars stored in DB

_logger = logging.getLogger("classifier")
_logger.setLevel(logging.INFO)


class GeminiNetworkError(RuntimeError):
    """Raised when the Gemini API cannot be reached due to DNS/network failure."""


_MODEL_PRICING = {
    "gemini-2.5-flash": {
        "input":  0.30 / 1_000_000,
        "output": 2.50 / 1_000_000,
    },
    "gemini-2.5-flash-lite": {
        "input":  0.10 / 1_000_000,
        "output": 0.40 / 1_000_000,
    },
}


def _get_pricing(model_name: str) -> dict:
    # Sort longest key first so "gemini-2.5-flash-lite" matches before "gemini-2.5-flash".
    for key in sorted(_MODEL_PRICING, key=len, reverse=True):
        if key in model_name:
            return _MODEL_PRICING[key]
    _logger.warning("Unknown model %r for pricing, defaulting to flash rates", model_name)
    return _MODEL_PRICING["gemini-2.5-flash"]

_FAILURE_RESULT = {
    "is_whv_friendly":  -1,
    "visa_sponsorship": 0,
    "accommodation":    0,
    "urgency":          "normal",
    "classifier_note":  "classification failed after retries",
    "job_type":         "other",
    "regional_area":    None,
}

PROMPT_TEMPLATE = """Classify this Australian job for WHV (subclass 417/462) holders.
Return ONLY valid JSON with these keys:
- is_whv_friendly: 1=suitable (short-term/casual/seasonal/backpacker), 0=not
- visa_sponsorship: 1=mentioned, 0=not
- accommodation: 1=provided, 0=not
- urgency: "high"(ASAP/immediately), "normal", or "low"
- note: max 10 words explaining is_whv_friendly decision
- job_type: "hospitality"|"hotel"|"retail"|"farm"|"office"|"other"
- regional_area: true=regional AU (Cairns/Darwin/Townsville/Broome/Alice Springs/Hobart/rural), false=major city (Sydney/Melbourne/Brisbane/Perth/Adelaide/Canberra/Gold Coast), null=unknown

Rule: if URL contains "backpackerjobboard.com.au" → is_whv_friendly=1 unless title/desc says otherwise.

Title: {title}
Company: {company}
Location: {location}
URL: {url}
Description: {description}"""

_TEMPLATE_STATIC_CHARS = len(PROMPT_TEMPLATE) - len("{title}{company}{location}{url}{description}")

_WHV_KEYWORDS = (
    "visa", "sponsorship", "accommodation", "backpacker", "working holiday",
    "casual", "seasonal", "immediate", "asap", "regional", "rural",
    "harvest", "farm", "hostel", "provided", "on-site",
)

_VALID_JOB_TYPES = ("hospitality", "hotel", "retail", "farm", "office", "other")


def _extract_relevant_excerpt(description: str, max_chars: int = _DESC_TRUNCATE_LEN) -> str:
    """Return sentences containing WHV-relevant keywords, else first max_chars."""
    if not description:
        return ""
    if len(description) <= 200:
        return description
    sentences = [s.strip() for s in description.replace("\n", " ").split(".") if s.strip()]
    relevant = [s for s in sentences if any(kw in s.lower() for kw in _WHV_KEYWORDS)]
    if relevant:
        return ". ".join(relevant[:6])[:max_chars]
    return description[:max_chars]


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


def _is_network_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(
        marker in s
        for marker in (
            "could not contact dns servers",
            "cannot connect to host",
            "temporary failure in name resolution",
            "name or service not known",
            "getaddrinfo failed",
            "nodename nor servname provided",
            "network is unreachable",
            "connection refused",
            "connection reset",
            "timed out",
            "timeout",
        )
    )


def _format_network_error_report(reason: str, affected_jobs: int | None = None) -> str:
    job_line = ""
    if affected_jobs is not None:
        job_line = f"\nAffected jobs: {affected_jobs}"
    return (
        "\nGemini classification stopped because the API cannot be reached.\n"
        f"Problem: {reason}\n"
        f"Host: {_GEMINI_API_HOST}:443"
        f"{job_line}\n"
        "Impact: fetched jobs were saved, but AI classification did not run.\n"
        "Next step: check DNS/internet/VPN/firewall settings, then run getdata.bat again.\n"
    )


def _resolve_host_error(host: str, timeout: float = _DNS_CHECK_TIMEOUT_SECONDS) -> str | None:
    _result: list = [None]
    _done = threading.Event()

    def _lookup():
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except Exception as exc:
            _result[0] = exc
        finally:
            _done.set()

    t = threading.Thread(target=_lookup, daemon=True)
    t.start()
    if _done.wait(timeout=timeout):
        exc = _result[0]
        return None if exc is None else f"DNS lookup for {host} failed: {exc}"
    return f"DNS lookup for {host} timed out after {timeout:.1f}s"


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
        description=_extract_relevant_excerpt(job.get("description") or ""),
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
                elif _is_network_error(exc):
                    err_stats["network_errors"] += 1
                    raise GeminiNetworkError(
                        _format_network_error_report(
                            f"network/DNS error while classifying job {job.get('id')!r}: {exc}"
                        )
                    ) from exc
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
    dns_error = _resolve_host_error(_GEMINI_API_HOST)
    if dns_error:
        raise GeminiNetworkError(
            _format_network_error_report(dns_error, affected_jobs=len(jobs))
        )

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
    err_stats = {
        "503_count": 0,
        "network_errors": 0,
        "max_backoff": 0.0,
        "retried_jobs": 0,
        "retried_success": 0,
    }

    _logger.info(
        "=== Classification batch start — %d jobs | template ~%d chars static | concurrency: %d→%d over %.0fs ===",
        len(jobs), _TEMPLATE_STATIC_CHARS, _RAMP_INITIAL, semaphore_limit, _RAMP_SECONDS,
    )

    tasks = [
        asyncio.create_task(
            _process_job(client, model_name, semaphore, job, on_result,
                         token_counts, api_call_counter, err_stats)
        )
        for job in jobs
    ]
    try:
        results = await asyncio.gather(*tasks)
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
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
        pricing = _get_pricing(model_name)
        cost = total_input * pricing["input"] + total_output * pricing["output"]
        token_suffix = f" | tokens in/out: {total_input:,}/{total_output:,} | cost: ${cost:.4f} USD"
    else:
        token_suffix = ""

    _logger.info(
        "=== Batch complete — jobs: %d | API calls: %d | retries: %d | "
        "503_errors: %d | network_errors: %d | max_backoff: %.1fs | retry_success_rate: %s | "
        "classified: %d | failed: %d%s ===",
        len(jobs), total_calls, retries,
        err_stats["503_count"], err_stats["network_errors"],
        err_stats["max_backoff"], retry_success_rate,
        classified, skipped, token_suffix,
    )

    return classified, skipped


def classify_batch(config: dict, jobs: list[dict],
                   on_result=None) -> tuple[int, int]:
    # aiodns/c-ares fails on some Windows DNS configs; force ThreadedResolver
    # (socket.getaddrinfo) by patching the name TCPConnector.__init__ reads.
    import aiohttp
    import aiohttp.connector
    import aiohttp.resolver
    # Patch aiodns/c-ares which fails on some Windows DNS configs.
    # Tested with aiohttp 3.9.x — re-verify after aiohttp upgrades.
    if hasattr(aiohttp.connector, "DefaultResolver"):
        aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver
    else:
        _logger.warning(
            "aiohttp.connector.DefaultResolver not found — DNS patch skipped "
            "(aiohttp internals may have changed; version: %s)",
            getattr(aiohttp, "__version__", "unknown"),
        )
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(_classify_all(config, jobs, on_result))
