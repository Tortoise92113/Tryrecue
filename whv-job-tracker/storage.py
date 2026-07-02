import re
import sqlite3
import contextlib
from pathlib import Path

DB_PATH      = Path(__file__).parent / "jobs.db"
ANALYTICS_DB = Path(__file__).parent / "analytics.db"
UNCLASSIFIED_JOB_TYPE = "unknown"
OTHER_REGION_KEY = "Others"
MIN_CITY_JOBS    = 10
OTHER_CITIES_KEY     = "Other Cities"
MIN_OTHER_CITY_JOBS  = 3

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT,
    location        TEXT,
    city            TEXT,
    description     TEXT,
    url             TEXT,
    salary_min      REAL,
    salary_max      REAL,
    posted_at       TEXT,
    fetched_at      TEXT NOT NULL,
    is_whv_friendly INTEGER,
    visa_sponsorship INTEGER,
    accommodation   INTEGER,
    urgency         TEXT,
    classifier_note TEXT,
    job_type        TEXT,
    regional_area   INTEGER,
    is_favorite     INTEGER DEFAULT 0,
    favorited_at    TEXT,
    is_deleted      INTEGER DEFAULT 0,
    deleted_at      TEXT,
    is_purged       INTEGER DEFAULT 0,
    last_seen_at    TEXT,
    missed_count    INTEGER NOT NULL DEFAULT 0,
    delisted_at     TEXT
);

CREATE TABLE IF NOT EXISTS job_user_states (
    job_id      TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    state       TEXT NOT NULL DEFAULT 'new',
    note        TEXT,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_source        ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_city          ON jobs(city);
CREATE INDEX IF NOT EXISTS idx_jobs_fetched_at    ON jobs(fetched_at);
CREATE INDEX IF NOT EXISTS idx_jobs_is_whv        ON jobs(is_whv_friendly);
CREATE INDEX IF NOT EXISTS idx_jobs_is_deleted    ON jobs(is_deleted);
CREATE INDEX IF NOT EXISTS idx_jobs_is_favorite   ON jobs(is_favorite);
CREATE INDEX IF NOT EXISTS idx_jobs_is_purged     ON jobs(is_purged);
CREATE INDEX IF NOT EXISTS idx_states_state       ON job_user_states(state);
CREATE INDEX IF NOT EXISTS idx_jobs_deleted_fetched ON jobs(is_deleted, fetched_at DESC);
"""


@contextlib.contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Add columns introduced after initial schema (safe to re-run)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "regional_area" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN regional_area INTEGER")
    if "job_type" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN job_type TEXT")
    if "is_favorite" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN is_favorite INTEGER DEFAULT 0")
    if "favorited_at" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN favorited_at TEXT DEFAULT NULL")
    if "is_deleted" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN is_deleted INTEGER DEFAULT 0")
    if "deleted_at" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN deleted_at TEXT DEFAULT NULL")
    if "is_purged" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN is_purged INTEGER DEFAULT 0")
    if "last_seen_at" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN last_seen_at TEXT")
    if "missed_count" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN missed_count INTEGER NOT NULL DEFAULT 0")
    if "delisted_at" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN delisted_at TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_delisted ON jobs(delisted_at)")


def _dedup_batch(jobs: list[dict]) -> list[dict]:
    """Remove within-batch duplicates by (title, company, location), keeping first seen."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for job in jobs:
        key = (
            (job.get("title") or "").lower().strip(),
            (job.get("company") or "").lower().strip(),
            (job.get("location") or job.get("city") or "").lower().strip(),
        )
        if key not in seen:
            seen.add(key)
            out.append(job)
    return out


def soft_delete_job(job_id: str) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET is_deleted = 1, deleted_at = ? WHERE id = ?",
            (now, job_id),
        )


def upsert_jobs(jobs: list[dict]) -> int:
    """Insert new jobs, skip duplicates by id or title+company+location. Returns count inserted."""
    if not jobs:
        return 0
    jobs = _dedup_batch(jobs)

    from datetime import datetime, timezone, timedelta
    one_month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    job_ids = [j["id"] for j in jobs if j.get("id")]

    insert_sql = """
        INSERT OR IGNORE INTO jobs (
            id, source, title, company, location, city,
            description, url, salary_min, salary_max,
            posted_at, fetched_at, last_seen_at,
            is_whv_friendly, visa_sponsorship, accommodation,
            urgency, classifier_note, job_type, regional_area
        ) VALUES (
            :id, :source, :title, :company, :location, :city,
            :description, :url, :salary_min, :salary_max,
            :posted_at, :fetched_at, :last_seen_at,
            :is_whv_friendly, :visa_sponsorship, :accommodation,
            :urgency, :classifier_note, :job_type, :regional_area
        )
    """
    defaults = dict(
        company=None, location=None, city=None, description=None,
        url=None, salary_min=None, salary_max=None, posted_at=None,
        is_whv_friendly=None, visa_sponsorship=None, accommodation=None,
        urgency=None, classifier_note=None, job_type=None, regional_area=None,
    )
    rows = [{**defaults, **j, "last_seen_at": j.get("fetched_at")} for j in jobs]
    with get_conn() as conn:
        if job_ids:
            placeholders = ",".join("?" * len(job_ids))
            conn.execute(
                f"UPDATE jobs SET is_deleted = 0, deleted_at = NULL "
                f"WHERE id IN ({placeholders}) AND is_deleted = 1 AND is_purged = 0 "
                f"AND deleted_at < ? AND (is_whv_friendly IS NULL OR is_whv_friendly != 0)",
                (*job_ids, one_month_ago),
            )
        cur = conn.executemany(insert_sql, rows)
        return cur.rowcount


def update_classifier(job_id: str, result: dict):
    sql = """
        UPDATE jobs SET
            is_whv_friendly  = :is_whv_friendly,
            visa_sponsorship = :visa_sponsorship,
            accommodation    = :accommodation,
            urgency          = :urgency,
            classifier_note  = :classifier_note,
            job_type         = :job_type,
            regional_area    = :regional_area
        WHERE id = :job_id
    """
    with get_conn() as conn:
        conn.execute(sql, {**result, "job_id": job_id})


def get_unclassified_job_ids() -> set[str]:
    """Return IDs of jobs not yet classified or that failed classification (is_whv_friendly = -1)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE (is_whv_friendly IS NULL OR is_whv_friendly = -1)"
            " AND delisted_at IS NULL"
        ).fetchall()
    return {row["id"] for row in rows}


def get_unclassified_jobs() -> list[dict]:
    """Return full records for jobs not yet classified or that failed classification."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE (is_whv_friendly IS NULL OR is_whv_friendly = -1)"
            " AND delisted_at IS NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def soft_delete_non_whv_jobs() -> int:
    """Soft-delete non-WHV-friendly jobs so they aren't re-classified when re-scraped."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        return conn.execute(
            "UPDATE jobs SET is_deleted = 1, deleted_at = ? "
            "WHERE is_whv_friendly = 0 AND is_deleted = 0 AND delisted_at IS NULL",
            (now,)
        ).rowcount


def set_user_state(job_id: str, state: str, note: str = None):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    sql = """
        INSERT INTO job_user_states (job_id, state, note, updated_at)
        VALUES (:job_id, :state, :note, :updated_at)
        ON CONFLICT(job_id) DO UPDATE SET
            state      = excluded.state,
            note       = excluded.note,
            updated_at = excluded.updated_at
    """
    with get_conn() as conn:
        conn.execute(sql, {"job_id": job_id, "state": state, "note": note, "updated_at": now})


def visible_job_clause(exclude_urgent: bool = False) -> str:
    """SQL WHERE fragment for a job counting toward a city's visible total.

    Used wherever code needs to decide "how many jobs does this city have"
    for bucketing/threshold purposes (the 'Others' small-city cutoff in both
    the homepage job-list filter and the analysis pie chart). Kept in one
    place so the two stay consistent — matches the homepage's grand_total
    semantics (whv_only + exclude_urgent) that the pie chart total is
    expected to agree with.
    """
    urgent_clause = " AND (urgency IS NULL OR urgency != 'high')" if exclude_urgent else ""
    return (
        "city IS NOT NULL AND is_whv_friendly = 1"
        " AND is_deleted = 0 AND delisted_at IS NULL" + urgent_clause
    )


def _build_jobs_where(
    city: str = None, whv_only: bool = False,
    state_filter: str = None, job_types: list[str] = None,
    exclude_urgent: bool = False, source: str = None,
    regional_area: int = None,
) -> tuple[list, dict]:
    clauses, params = ["j.is_deleted = 0", "j.delisted_at IS NULL"], {}
    if city == OTHER_REGION_KEY:
        clauses.append(f"""j.city IN (
            SELECT city FROM jobs
            WHERE {visible_job_clause(exclude_urgent)}
            GROUP BY city HAVING COUNT(*) < :other_min
        )""")
        params["other_min"] = MIN_CITY_JOBS
    elif city:
        clauses.append("j.city = :city")
        params["city"] = city
    if whv_only:
        clauses.append("j.is_whv_friendly = 1")
    if state_filter:
        clauses.append("COALESCE(s.state, 'new') = :state")
        params["state"] = state_filter
    if exclude_urgent:
        clauses.append("(j.urgency IS NULL OR j.urgency != 'high')")
    if job_types:
        placeholders = ",".join(f":jt{i}" for i in range(len(job_types)))
        clauses.append(f"j.job_type IN ({placeholders})")
        for i, jt in enumerate(job_types):
            params[f"jt{i}"] = jt
    if source:
        clauses.append("j.source = :source")
        params["source"] = source
    if regional_area is not None:
        clauses.append("j.regional_area = :regional_area")
        params["regional_area"] = regional_area
    clauses.append("COALESCE(s.state, 'new') != 'hidden'")
    return clauses, params


def count_jobs(
    city: str = None, whv_only: bool = False,
    state_filter: str = None, job_types: list[str] = None,
    exclude_urgent: bool = False, source: str = None,
    regional_area: int = None,
) -> int:
    clauses, params = _build_jobs_where(
        city=city, whv_only=whv_only, state_filter=state_filter,
        job_types=job_types, exclude_urgent=exclude_urgent,
        source=source, regional_area=regional_area,
    )
    where = " AND ".join(clauses)
    sql = f"""
        SELECT COUNT(*)
        FROM jobs j
        LEFT JOIN job_user_states s ON j.id = s.job_id
        WHERE {where}
    """
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()[0]


def get_jobs(city: str = None, whv_only: bool = False,
             state_filter: str = None, job_types: list[str] = None,
             exclude_urgent: bool = False, source: str = None,
             regional_area: int = None,
             limit: int = 200, offset: int = 0) -> list[sqlite3.Row]:
    clauses, params = _build_jobs_where(
        city=city, whv_only=whv_only, state_filter=state_filter,
        job_types=job_types, exclude_urgent=exclude_urgent,
        source=source, regional_area=regional_area,
    )
    where = " AND ".join(clauses)
    sql = f"""
        SELECT j.*, COALESCE(s.state, 'new') AS user_state, s.note AS user_note
        FROM jobs j
        LEFT JOIN job_user_states s ON j.id = s.job_id
        WHERE {where}
        ORDER BY j.fetched_at DESC
        LIMIT :limit OFFSET :offset
    """
    params["limit"] = limit
    params["offset"] = offset
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def get_deleted_jobs() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("""
            SELECT id, title, location, city, job_type, deleted_at
            FROM jobs WHERE is_deleted = 1 AND is_purged = 0
            ORDER BY deleted_at DESC
        """).fetchall()


def restore_job(job_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET is_deleted = 0, deleted_at = NULL WHERE id = ?",
            (job_id,),
        )


def purge_jobs(job_ids: list[str]) -> int:
    if not job_ids:
        return 0
    with get_conn() as conn:
        placeholders = ",".join("?" * len(job_ids))
        cur = conn.execute(
            f"UPDATE jobs SET is_purged = 1 WHERE id IN ({placeholders}) AND is_deleted = 1",
            job_ids,
        )
    return cur.rowcount


def permanent_delete_jobs() -> tuple[int, list[str]]:
    """Scheduled cleanup: hard-delete all purged jobs older than 30 days."""
    from datetime import datetime, timezone, timedelta
    one_month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE is_purged = 1 AND deleted_at < ?",
            (one_month_ago,),
        ).fetchall()
        to_delete = [r["id"] for r in rows]
        if to_delete:
            placeholders = ",".join("?" * len(to_delete))
            conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", to_delete)
    return len(to_delete), []


def get_new_jobs_since(since_iso: str) -> list[sqlite3.Row]:
    sql = """
        SELECT j.*, COALESCE(s.state, 'new') AS user_state
        FROM jobs j
        LEFT JOIN job_user_states s ON j.id = s.job_id
        WHERE j.fetched_at >= :since
          AND j.delisted_at IS NULL
          AND COALESCE(s.state, 'new') = 'new'
        ORDER BY j.fetched_at DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, {"since": since_iso}).fetchall()


def fetched_today() -> bool:
    """Return True if any job was fetched today (compared in local time)."""
    from datetime import date
    today = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE date(fetched_at, 'localtime') = ? LIMIT 1",
            (today,)
        ).fetchone()
    return row is not None


def source_fetched_today(source: str) -> bool:
    """Return True if any job from this source was fetched today."""
    from datetime import date
    today = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE source = ? AND date(fetched_at, 'localtime') = ? LIMIT 1",
            (source, today)
        ).fetchone()
    return row is not None


def get_todays_whv_jobs() -> list[dict]:
    """Return all WHV-friendly, non-deleted jobs fetched today (local time)."""
    from datetime import date
    today = date.today().isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, title, company, location, city, job_type,
                   salary_min, salary_max, url, is_whv_friendly,
                   regional_area, source, urgency, classifier_note
            FROM jobs
            WHERE date(fetched_at, 'localtime') = ?
              AND is_whv_friendly = 1
              AND is_deleted = 0
              AND delisted_at IS NULL
            ORDER BY city, job_type, title
        """, (today,)).fetchall()
    return [dict(r) for r in rows]


def get_all_whv_jobs() -> list[dict]:
    """Return all active (non-deleted, non-delisted) WHV-friendly jobs."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, title, company, location, city, job_type,
                   salary_min, salary_max, url, is_whv_friendly,
                   regional_area, source, urgency, classifier_note
            FROM jobs
            WHERE is_whv_friendly = 1
              AND is_deleted = 0
              AND delisted_at IS NULL
            ORDER BY city, job_type, title
        """).fetchall()
    return [dict(r) for r in rows]


_SUBURB_TO_CITY: dict[str, str] = {
    # Sydney metro
    "City of Sydney, NSW": "Sydney", "Concord, NSW": "Sydney",
    "Villawood, NSW": "Sydney", "Milperra, NSW": "Sydney",
    "Parramatta, NSW": "Sydney", "Blacktown, NSW": "Sydney",
    "Liverpool, NSW": "Sydney", "Bankstown, NSW": "Sydney",
    "Fairfield, NSW": "Sydney", "Campbelltown, NSW": "Sydney",
    "Penrith, NSW": "Sydney", "Auburn, NSW": "Sydney",
    "Hurstville, NSW": "Sydney", "Rockdale, NSW": "Sydney",
    "Sutherland, NSW": "Sydney", "Cronulla, NSW": "Sydney",
    "Miranda, NSW": "Sydney", "Chatswood, NSW": "Sydney",
    "Hornsby, NSW": "Sydney", "Ryde, NSW": "Sydney",
    "Manly, NSW": "Sydney", "Bondi, NSW": "Sydney",
    "Randwick, NSW": "Sydney", "Marrickville, NSW": "Sydney",
    "Burwood, NSW": "Sydney", "Strathfield, NSW": "Sydney",
    "Ashfield, NSW": "Sydney", "Leichhardt, NSW": "Sydney",
    "Newtown, NSW": "Sydney", "Redfern, NSW": "Sydney",
    "Surry Hills, NSW": "Sydney", "Pyrmont, NSW": "Sydney",
    "Rozelle, NSW": "Sydney", "Balmain, NSW": "Sydney",
    "Drummoyne, NSW": "Sydney", "Five Dock, NSW": "Sydney",
    "Homebush, NSW": "Sydney", "Lidcombe, NSW": "Sydney",
    "Granville, NSW": "Sydney", "Merrylands, NSW": "Sydney",
    "Guildford, NSW": "Sydney", "Wentworthville, NSW": "Sydney",
    "Seven Hills, NSW": "Sydney", "Toongabbie, NSW": "Sydney",
    "Kings Langley, NSW": "Sydney", "Wetherill Park, NSW": "Sydney",
    "Smithfield, NSW": "Sydney", "Cabramatta, NSW": "Sydney",
    "Canley Vale, NSW": "Sydney", "Bonnyrigg, NSW": "Sydney",
    "Moorebank, NSW": "Sydney", "Casula, NSW": "Sydney",
    "Prestons, NSW": "Sydney", "Edmondson Park, NSW": "Sydney",
    "Ingleburn, NSW": "Sydney", "Minto, NSW": "Sydney",
    "Leumeah, NSW": "Sydney", "Narellan, NSW": "Sydney",
    "Camden, NSW": "Sydney", "Macquarie Park, NSW": "Sydney",
    "North Ryde, NSW": "Sydney", "Meadowbank, NSW": "Sydney",
    "Rhodes, NSW": "Sydney", "Lane Cove, NSW": "Sydney",
    "Artarmon, NSW": "Sydney", "St Leonards, NSW": "Sydney",
    "Crows Nest, NSW": "Sydney", "North Sydney, NSW": "Sydney",
    "Neutral Bay, NSW": "Sydney", "Mosman, NSW": "Sydney",
    "Cremorne, NSW": "Sydney", "Kirrawee, NSW": "Sydney",
    "Engadine, NSW": "Sydney", "Menai, NSW": "Sydney",
    "Caringbah, NSW": "Sydney",

    # Melbourne metro
    "Derrimut, VIC": "Melbourne", "Lilydale, VIC": "Melbourne",
    "Carlton, VIC": "Melbourne", "Footscray, VIC": "Melbourne",
    "Dandenong, VIC": "Melbourne", "Frankston, VIC": "Melbourne",
    "Ringwood, VIC": "Melbourne", "Box Hill, VIC": "Melbourne",
    "Doncaster, VIC": "Melbourne", "Werribee, VIC": "Melbourne",
    "Sunshine, VIC": "Melbourne", "Broadmeadows, VIC": "Melbourne",
    "Thomastown, VIC": "Melbourne", "Preston, VIC": "Melbourne",
    "Brunswick, VIC": "Melbourne", "Fitzroy, VIC": "Melbourne",
    "Richmond, VIC": "Melbourne", "St Kilda, VIC": "Melbourne",
    "Prahran, VIC": "Melbourne", "Oakleigh, VIC": "Melbourne",
    "Clayton, VIC": "Melbourne", "Springvale, VIC": "Melbourne",
    "Cheltenham, VIC": "Melbourne", "Moorabbin, VIC": "Melbourne",
    "Bentleigh, VIC": "Melbourne", "Mooroolbark, VIC": "Melbourne",
    "Croydon, VIC": "Melbourne", "Bayswater, VIC": "Melbourne",
    "Ferntree Gully, VIC": "Melbourne", "Knox, VIC": "Melbourne",
    "Boronia, VIC": "Melbourne", "Wantirna, VIC": "Melbourne",
    "Rowville, VIC": "Melbourne", "Scoresby, VIC": "Melbourne",
    "Knoxfield, VIC": "Melbourne", "Narre Warren, VIC": "Melbourne",
    "Berwick, VIC": "Melbourne", "Cranbourne, VIC": "Melbourne",
    "Pakenham, VIC": "Melbourne", "Hallam, VIC": "Melbourne",
    "Hampton Park, VIC": "Melbourne", "Fountain Gate, VIC": "Melbourne",
    "Epping, VIC": "Melbourne", "Lalor, VIC": "Melbourne",
    "Mill Park, VIC": "Melbourne", "South Morang, VIC": "Melbourne",
    "Bundoora, VIC": "Melbourne", "Greensborough, VIC": "Melbourne",
    "Watsonia, VIC": "Melbourne", "Eltham, VIC": "Melbourne",
    "Diamond Creek, VIC": "Melbourne", "Reservoir, VIC": "Melbourne",
    "Northcote, VIC": "Melbourne", "Thornbury, VIC": "Melbourne",
    "Coburg, VIC": "Melbourne", "Pascoe Vale, VIC": "Melbourne",
    "Glenroy, VIC": "Melbourne", "Fawkner, VIC": "Melbourne",
    "Campbellfield, VIC": "Melbourne", "Somerton, VIC": "Melbourne",
    "Tullamarine, VIC": "Melbourne", "Airport West, VIC": "Melbourne",
    "Essendon, VIC": "Melbourne", "Moonee Ponds, VIC": "Melbourne",
    "Ascot Vale, VIC": "Melbourne", "Flemington, VIC": "Melbourne",
    "Kensington, VIC": "Melbourne", "West Melbourne, VIC": "Melbourne",
    "Port Melbourne, VIC": "Melbourne", "South Melbourne, VIC": "Melbourne",
    "Albert Park, VIC": "Melbourne", "Middle Park, VIC": "Melbourne",
    "South Yarra, VIC": "Melbourne", "Toorak, VIC": "Melbourne",
    "Armadale, VIC": "Melbourne", "Malvern, VIC": "Melbourne",
    "Glen Waverley, VIC": "Melbourne", "Mount Waverley, VIC": "Melbourne",
    "Nunawading, VIC": "Melbourne", "Mitcham, VIC": "Melbourne",
    "Vermont, VIC": "Melbourne", "Chirnside Park, VIC": "Melbourne",
    "Healesville, VIC": "Melbourne",

    # Brisbane metro
    "South Brisbane, QLD": "Brisbane", "Bowen Hills, QLD": "Brisbane",
    "Heathwood, QLD": "Brisbane", "Underwood, QLD": "Brisbane",
    "Thornlands, QLD": "Brisbane", "Cleveland, QLD": "Brisbane",
    "Chermside, QLD": "Brisbane", "Carindale, QLD": "Brisbane",
    "Indooroopilly, QLD": "Brisbane", "Toowong, QLD": "Brisbane",
    "Nundah, QLD": "Brisbane", "Wynnum, QLD": "Brisbane",
    "Stafford, QLD": "Brisbane", "Aspley, QLD": "Brisbane",
    "Strathpine, QLD": "Brisbane", "Springwood, QLD": "Brisbane",
    "Slacks Creek, QLD": "Brisbane", "Richlands, QLD": "Brisbane",
    "Acacia Ridge, QLD": "Brisbane", "Eight Mile Plains, QLD": "Brisbane",
    "Fortitude Valley, QLD": "Brisbane", "West End, QLD": "Brisbane",
    "New Farm, QLD": "Brisbane", "Newstead, QLD": "Brisbane",
    "Teneriffe, QLD": "Brisbane", "Albion, QLD": "Brisbane",
    "Woolloongabba, QLD": "Brisbane", "Greenslopes, QLD": "Brisbane",
    "Holland Park, QLD": "Brisbane", "Mount Gravatt, QLD": "Brisbane",
    "Mansfield, QLD": "Brisbane", "Rochedale, QLD": "Brisbane",
    "Sunnybank, QLD": "Brisbane", "Moorooka, QLD": "Brisbane",
    "Yeronga, QLD": "Brisbane", "Rocklea, QLD": "Brisbane",
    "Archerfield, QLD": "Brisbane", "Coopers Plains, QLD": "Brisbane",
    "Salisbury, QLD": "Brisbane", "Nathan, QLD": "Brisbane",
    "Tarragindi, QLD": "Brisbane", "Annerley, QLD": "Brisbane",
    "Fairfield, QLD": "Brisbane", "Chelmer, QLD": "Brisbane",
    "Sherwood, QLD": "Brisbane", "Graceville, QLD": "Brisbane",
    "Corinda, QLD": "Brisbane", "Oxley, QLD": "Brisbane",
    "Darra, QLD": "Brisbane", "Wacol, QLD": "Brisbane",
    "Inala, QLD": "Brisbane", "Forest Lake, QLD": "Brisbane",
    "Durack, QLD": "Brisbane", "Doolandella, QLD": "Brisbane",
    "Pallara, QLD": "Brisbane", "Willawong, QLD": "Brisbane",
    "Larapinta, QLD": "Brisbane", "Algester, QLD": "Brisbane",
    "Calamvale, QLD": "Brisbane", "Parkinson, QLD": "Brisbane",
    "Robertson, QLD": "Brisbane", "Runcorn, QLD": "Brisbane",
    "Kuraby, QLD": "Brisbane", "Drewvale, QLD": "Brisbane",
    "Stretton, QLD": "Brisbane", "Macgregor, QLD": "Brisbane",
    "Upper Mount Gravatt, QLD": "Brisbane", "Wishart, QLD": "Brisbane",
    "Mackenzie, QLD": "Brisbane", "Belmont, QLD": "Brisbane",
    "Tingalpa, QLD": "Brisbane", "Hemmant, QLD": "Brisbane",
    "Lytton, QLD": "Brisbane", "Pinkenba, QLD": "Brisbane",
    "Eagle Farm, QLD": "Brisbane", "Northgate, QLD": "Brisbane",
    "Banyo, QLD": "Brisbane", "Nudgee, QLD": "Brisbane",
    "Geebung, QLD": "Brisbane", "Zillmere, QLD": "Brisbane",
    "Boondall, QLD": "Brisbane", "Taigum, QLD": "Brisbane",
    "Fitzgibbon, QLD": "Brisbane", "Bracken Ridge, QLD": "Brisbane",
    "Bald Hills, QLD": "Brisbane", "Sandgate, QLD": "Brisbane",
    "Brighton, QLD": "Brisbane", "Deagon, QLD": "Brisbane",
    "Shorncliffe, QLD": "Brisbane", "Clontarf, QLD": "Brisbane",
    "Redcliffe, QLD": "Brisbane", "Woody Point, QLD": "Brisbane",
    "Scarborough, QLD": "Brisbane",

    # Gold Coast
    "Nerang, QLD": "Gold Coast", "Southport, QLD": "Gold Coast",
    "Surfers Paradise, QLD": "Gold Coast", "Broadbeach, QLD": "Gold Coast",
    "Robina, QLD": "Gold Coast", "Coomera, QLD": "Gold Coast",
    "Helensvale, QLD": "Gold Coast", "Labrador, QLD": "Gold Coast",
    "Runaway Bay, QLD": "Gold Coast", "Bundall, QLD": "Gold Coast",
    "Ashmore, QLD": "Gold Coast", "Molendinar, QLD": "Gold Coast",
    "Arundel, QLD": "Gold Coast", "Parkwood, QLD": "Gold Coast",
    "Carrara, QLD": "Gold Coast", "Merrimac, QLD": "Gold Coast",
    "Varsity Lakes, QLD": "Gold Coast", "Mudgeeraba, QLD": "Gold Coast",
    "Tallai, QLD": "Gold Coast", "Worongary, QLD": "Gold Coast",
    "Highland Park, QLD": "Gold Coast", "Bonogin, QLD": "Gold Coast",
    "Reedy Creek, QLD": "Gold Coast", "Burleigh Heads, QLD": "Gold Coast",
    "Burleigh Waters, QLD": "Gold Coast", "Elanora, QLD": "Gold Coast",
    "Palm Beach, QLD": "Gold Coast", "Currumbin, QLD": "Gold Coast",
    "Tugun, QLD": "Gold Coast", "Coolangatta, QLD": "Gold Coast",
    "Bilinga, QLD": "Gold Coast",
    # Tweed Heads is across the border in NSW but functionally part of Gold Coast
    "Tweed Heads, NSW": "Gold Coast",

    # Perth metro
    "Kenwick, WA": "Perth", "Joondalup, WA": "Perth",
    "Fremantle, WA": "Perth", "Rockingham, WA": "Perth",
    "Midland, WA": "Perth", "Cannington, WA": "Perth",
    "Morley, WA": "Perth", "Osborne Park, WA": "Perth",
    "Malaga, WA": "Perth", "Balcatta, WA": "Perth",
    "Victoria Park, WA": "Perth", "Subiaco, WA": "Perth",
    "Claremont, WA": "Perth", "Cottesloe, WA": "Perth",
    "Stirling, WA": "Perth", "Innaloo, WA": "Perth",
    "Karrinyup, WA": "Perth", "Scarborough, WA": "Perth",
    "Nollamara, WA": "Perth", "Westminster, WA": "Perth",
    "Mirrabooka, WA": "Perth", "Girrawheen, WA": "Perth",
    "Koondoola, WA": "Perth", "Wanneroo, WA": "Perth",
    "Wangara, WA": "Perth", "Landsdale, WA": "Perth",
    "Darch, WA": "Perth", "Madeley, WA": "Perth",
    "Hocking, WA": "Perth", "Quinns Rocks, WA": "Perth",
    "Clarkson, WA": "Perth", "Butler, WA": "Perth",
    "Yanchep, WA": "Perth", "Kwinana, WA": "Perth",
    "Baldivis, WA": "Perth", "Mandurah, WA": "Perth",
    "Spearwood, WA": "Perth", "Cockburn Central, WA": "Perth",
    "Success, WA": "Perth", "Bibra Lake, WA": "Perth",
    "Yangebup, WA": "Perth", "Hamilton Hill, WA": "Perth",
    "Henderson, WA": "Perth", "Coogee, WA": "Perth",
    "Munster, WA": "Perth", "O'Connor, WA": "Perth",
    "Beaconsfield, WA": "Perth", "White Gum Valley, WA": "Perth",
    "Hilton, WA": "Perth", "Palmyra, WA": "Perth",
    "Melville, WA": "Perth", "Willetton, WA": "Perth",
    "Murdoch, WA": "Perth", "Bull Creek, WA": "Perth",
    "Leeming, WA": "Perth", "Winthrop, WA": "Perth",
    "Kardinya, WA": "Perth", "Booragoon, WA": "Perth",
    "Applecross, WA": "Perth", "Mount Pleasant, WA": "Perth",
    "Ardross, WA": "Perth", "Dalkeith, WA": "Perth",
    "Nedlands, WA": "Perth", "Crawley, WA": "Perth",
    "Shenton Park, WA": "Perth", "Floreat, WA": "Perth",
    "City Beach, WA": "Perth", "Wembley, WA": "Perth",
    "Joondanna, WA": "Perth", "Mount Hawthorn, WA": "Perth",
    "Leederville, WA": "Perth", "North Perth, WA": "Perth",
    "Mount Lawley, WA": "Perth", "Inglewood, WA": "Perth",
    "Bedford, WA": "Perth", "Dianella, WA": "Perth",
    "Embleton, WA": "Perth", "Bayswater, WA": "Perth",
    "Bassendean, WA": "Perth", "Guildford, WA": "Perth",
    "Swan Valley, WA": "Perth", "High Wycombe, WA": "Perth",
    "Forrestfield, WA": "Perth", "Kalamunda, WA": "Perth",
    "Gosnells, WA": "Perth", "Maddington, WA": "Perth",
    "Thornlie, WA": "Perth", "Southern River, WA": "Perth",
    "Canning Vale, WA": "Perth", "Harrisdale, WA": "Perth",
    "Piara Waters, WA": "Perth", "Atwell, WA": "Perth",
    "Aubin Grove, WA": "Perth", "Hammond Park, WA": "Perth",
    "Banjup, WA": "Perth", "Jandakot, WA": "Perth",

    # Canberra
    "Manuka, ACT": "Canberra", "Barton, ACT": "Canberra",
    "Deakin, ACT": "Canberra", "Fyshwick, ACT": "Canberra",
    "Woden, ACT": "Canberra", "Belconnen, ACT": "Canberra",
    "Tuggeranong, ACT": "Canberra", "Gungahlin, ACT": "Canberra",
    "Braddon, ACT": "Canberra", "Civic, ACT": "Canberra",
    "Bruce, ACT": "Canberra", "Phillip, ACT": "Canberra",

    # Adelaide metro
    "Para Vista, SA": "Adelaide", "Elizabeth, SA": "Adelaide",
    "Salisbury, SA": "Adelaide", "Tea Tree Gully, SA": "Adelaide",
    "Modbury, SA": "Adelaide", "Glenelg, SA": "Adelaide",
    "Marion, SA": "Adelaide", "Noarlunga, SA": "Adelaide",
    "Morphett Vale, SA": "Adelaide", "Christies Beach, SA": "Adelaide",
    "Reynella, SA": "Adelaide", "Hackham, SA": "Adelaide",
    "Onkaparinga Hills, SA": "Adelaide", "Mawson Lakes, SA": "Adelaide",
    "Parafield, SA": "Adelaide", "Salisbury East, SA": "Adelaide",
    "Ingle Farm, SA": "Adelaide", "Para Hills, SA": "Adelaide",
    "Pooraka, SA": "Adelaide", "Gepps Cross, SA": "Adelaide",
    "Wingfield, SA": "Adelaide", "Mansfield Park, SA": "Adelaide",
    "Kilburn, SA": "Adelaide", "Enfield, SA": "Adelaide",
    "Blair Athol, SA": "Adelaide", "Northfield, SA": "Adelaide",
    "Valley View, SA": "Adelaide", "Ridgehaven, SA": "Adelaide",
    "Hope Valley, SA": "Adelaide", "Banksia Park, SA": "Adelaide",
    "Golden Grove, SA": "Adelaide", "Greenwith, SA": "Adelaide",
    "One Tree Hill, SA": "Adelaide",

    # Cairns
    "Woree, QLD": "Cairns", "Earlville, QLD": "Cairns",
    "Manunda, QLD": "Cairns", "Westcourt, QLD": "Cairns",
    "Parramatta Park, QLD": "Cairns", "Cairns North, QLD": "Cairns",
    "Portsmith, QLD": "Cairns", "Stratford, QLD": "Cairns",
    "Smithfield, QLD": "Cairns", "Caravonica, QLD": "Cairns",
    "Freshwater, QLD": "Cairns", "Redlynch, QLD": "Cairns",
    "Brinsmead, QLD": "Cairns", "Kewarra Beach, QLD": "Cairns",
    "Trinity Beach, QLD": "Cairns", "Trinity Park, QLD": "Cairns",
    "Yorkeys Knob, QLD": "Cairns", "Cairns City, QLD": "Cairns",
    "Edge Hill, QLD": "Cairns", "Bungalow, QLD": "Cairns",
    "Edmonton, QLD": "Cairns", "Bentley Park, QLD": "Cairns",
    "Manoora, QLD": "Cairns", "Mount Sheridan, QLD": "Cairns",
    "Gordonvale, QLD": "Cairns", "Whitfield, QLD": "Cairns",
    "Palm Cove, QLD": "Cairns", "Atherton, QLD": "Cairns",

    # Central Coast (NSW)
    "Tuggerah, NSW": "Central Coast", "Gosford, NSW": "Central Coast",
    "Wyong, NSW": "Central Coast", "Terrigal, NSW": "Central Coast",
    "The Entrance, NSW": "Central Coast", "Bateau Bay, NSW": "Central Coast",
    "Erina, NSW": "Central Coast", "Mingara, NSW": "Central Coast",
    "Toukley, NSW": "Central Coast", "Lake Haven, NSW": "Central Coast",

    # Newcastle (NSW)
    "Charlestown, NSW": "Newcastle", "Kotara, NSW": "Newcastle",
    "Gateshead, NSW": "Newcastle", "Jesmond, NSW": "Newcastle",
    "Wallsend, NSW": "Newcastle", "Broadmeadow, NSW": "Newcastle",
    "Mayfield, NSW": "Newcastle", "Hamilton, NSW": "Newcastle",
    "Waratah, NSW": "Newcastle", "Adamstown, NSW": "Newcastle",
    "Merewether, NSW": "Newcastle", "Islington, NSW": "Newcastle",

    # Townsville metro
    "Aitkenvale, QLD": "Townsville", "Kirwan, QLD": "Townsville",
    "South Townsville, QLD": "Townsville", "Pimlico, QLD": "Townsville",
    "Thuringowa Central, QLD": "Townsville", "Mundingburra, QLD": "Townsville",
    "Bohle, QLD": "Townsville", "Idalia, QLD": "Townsville",
    "Deeragun, QLD": "Townsville", "Douglas, QLD": "Townsville",
    "Garbutt, QLD": "Townsville", "Townsville City, QLD": "Townsville",
    "Nelly Bay, QLD": "Townsville",

    # Darwin metro (NT)
    "Casuarina, NT": "Darwin", "Winnellie, NT": "Darwin",
    "Fannie Bay, NT": "Darwin", "Parap, NT": "Darwin",
    "Rapid Creek, NT": "Darwin", "Stuart Park, NT": "Darwin",
    "Tiwi, NT": "Darwin", "Palmerston, NT": "Darwin",
    "Holtze, NT": "Darwin", "Berrimah, NT": "Darwin",
    "Bakewell, NT": "Darwin", "Coonawarra, NT": "Darwin",
    "Litchfield, NT": "Darwin",

    # Broome
    "Cable Beach, WA": "Broome", "Djugun, WA": "Broome",

    # Sydney metro (additional)
    "Sydney CBD, NSW": "Sydney",

    # Sunshine Coast
    "Maroochydore, QLD": "Sunshine Coast", "Caloundra, QLD": "Sunshine Coast",
    "Noosa Heads, QLD": "Sunshine Coast", "Kawana Waters, QLD": "Sunshine Coast",
    "Nambour, QLD": "Sunshine Coast", "Buderim, QLD": "Sunshine Coast",
    "Sippy Downs, QLD": "Sunshine Coast", "Mooloolaba, QLD": "Sunshine Coast",
    "Kawana, QLD": "Sunshine Coast", "Bokarina, QLD": "Sunshine Coast",
    "Birtinya, QLD": "Sunshine Coast", "Warana, QLD": "Sunshine Coast",
    "Currimundi, QLD": "Sunshine Coast", "Aroha, QLD": "Sunshine Coast",
    "Mountain Creek, QLD": "Sunshine Coast", "Kuluin, QLD": "Sunshine Coast",
    "Kunda Park, QLD": "Sunshine Coast", "Chevallum, QLD": "Sunshine Coast",
}


_LOCATION_STATE_RE = re.compile(r'^(.*?)\s+([A-Z]{2,3})(?:\s+\d{4})?$')


def backfill_jora_city() -> int:
    """Re-derive and normalise city from location text for active Jora jobs.

    Strips postcode and extracts the state abbreviation, then maps known
    "Suburb, STATE" pairs to their parent major city (e.g. "Villawood NSW 2163"
    → "Sydney"). Keying on suburb+state (rather than bare suburb name) avoids
    collisions between same-named suburbs in different states/cities
    (e.g. Fairfield NSW vs Fairfield QLD).
    """
    def _parse(location: str | None) -> str | None:
        if not location:
            return None
        cleaned = location.strip()
        m = _LOCATION_STATE_RE.match(cleaned)
        if not m:
            return cleaned or None
        suburb, state = m.group(1).strip(), m.group(2)
        if not suburb:
            return None
        return _SUBURB_TO_CITY.get(f"{suburb}, {state}", suburb)

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, location FROM jobs"
            " WHERE source = 'jora' AND delisted_at IS NULL AND is_deleted = 0"
        ).fetchall()
        updated = 0
        for row in rows:
            city = _parse(row["location"])
            if city:
                conn.execute("UPDATE jobs SET city = ? WHERE id = ?", (city, row["id"]))
                updated += 1
    return updated


def mark_jobs_seen(job_ids: set[str]) -> int:
    """Reset missed_count / revive specific jobs without penalising unseen jobs.

    Use for targeted catch-up scrapes where only one source is re-fetched.
    Calling update_job_presence would incorrectly increment missed_count for
    jobs from other sources that weren't part of this partial run.
    Returns the number of revived jobs (were delisted, now active).
    """
    from datetime import datetime, timezone
    if not job_ids:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    ph  = ",".join("?" * len(job_ids))
    ids = list(job_ids)
    with get_conn() as conn:
        revived = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE id IN ({ph}) AND delisted_at IS NOT NULL",
            ids,
        ).fetchone()[0]
        conn.execute(
            f"UPDATE jobs SET last_seen_at = ?, missed_count = 0, delisted_at = NULL"
            f" WHERE id IN ({ph})",
            [now, *ids],
        )
    return revived


def update_job_presence(seen_job_ids: set[str]) -> dict:
    """
    Call after every fetch run with the set of IDs actually retrieved.
    - Seen jobs: reset missed_count, update last_seen_at, revive if previously delisted.
    - Unseen active jobs: increment missed_count; delist when >= 3.
    Returns {"revived": N, "newly_delisted": M} for logging.
    """
    from datetime import datetime, timezone
    if not seen_job_ids:
        return {"revived": 0, "newly_delisted": 0}

    now  = datetime.now(timezone.utc).isoformat()
    ph   = ",".join("?" * len(seen_job_ids))
    seen = list(seen_job_ids)

    with get_conn() as conn:
        revived = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE id IN ({ph}) AND delisted_at IS NOT NULL",
            seen,
        ).fetchone()[0]

        conn.execute(
            f"UPDATE jobs SET last_seen_at = ?, missed_count = 0, delisted_at = NULL"
            f" WHERE id IN ({ph})",
            [now, *seen],
        )

        conn.execute(
            f"UPDATE jobs SET missed_count = missed_count + 1"
            f" WHERE id NOT IN ({ph}) AND is_deleted = 0 AND delisted_at IS NULL",
            seen,
        )

        newly_delisted = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE missed_count >= 3 AND delisted_at IS NULL AND is_deleted = 0"
        ).fetchone()[0]

        conn.execute(
            "UPDATE jobs SET delisted_at = ? WHERE missed_count >= 3 AND delisted_at IS NULL AND is_deleted = 0",
            (now,),
        )

    return {"revived": revived, "newly_delisted": newly_delisted}


if __name__ == "__main__":
    init_db()
    print(f"Database initialised at {DB_PATH}")
