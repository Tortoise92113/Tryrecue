import asyncio
import json
import sys

from google import genai

MAX_RETRIES = 3
RETRY_DELAY = 5
SEMAPHORE_LIMIT = 50

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
        "classifier_note":  str(result.get("note") or "")[:200],
        "job_type":         job_type,
        "regional_area":    regional_area,
    }


async def _process_job(client, model_name: str, semaphore: asyncio.Semaphore,
                       job: dict, on_result) -> bool:
    prompt = PROMPT_TEMPLATE.format(
        title=job.get("title", ""),
        company=job.get("company") or "Unknown",
        location=job.get("location") or job.get("city") or "Unknown",
        url=job.get("url") or "",
        description=(job.get("description") or "")[:1500],
    )

    for attempt in range(1, MAX_RETRIES + 1):
        async with semaphore:
            try:
                response = await client.aio.models.generate_content(
                    model=model_name, contents=prompt
                )
                text = response.text.strip()
                if text.startswith("```"):
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                result = _validate(json.loads(text.strip()))
                if on_result:
                    on_result(job["id"], result)
                return True
            except json.JSONDecodeError as exc:
                print(f"[classifier] JSON parse error (attempt {attempt}/{MAX_RETRIES}): {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"[classifier] error (attempt {attempt}/{MAX_RETRIES}): {exc}", file=sys.stderr)

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY * attempt)

    print(f"[classifier] gave up on job {job.get('id')!r} after {MAX_RETRIES} attempts", file=sys.stderr)
    return False


async def _classify_all(config: dict, jobs: list[dict], on_result) -> tuple[int, int]:
    client = genai.Client(api_key=config["gemini"]["api_key"])
    model_name = config["gemini"]["model"]
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)

    results = await asyncio.gather(
        *[_process_job(client, model_name, semaphore, job, on_result) for job in jobs]
    )

    classified = sum(results)
    skipped = len(results) - classified
    print(f"[classifier] done — classified: {classified}, skipped: {skipped}", file=sys.stderr)
    return classified, skipped


def classify_batch(config: dict, jobs: list[dict],
                   on_result=None) -> tuple[int, int]:
    return asyncio.run(_classify_all(config, jobs, on_result))
