import sqlite3
import contextlib
from pathlib import Path

DB_PATH      = Path(__file__).parent / "jobs.db"
ANALYTICS_DB = Path(__file__).parent / "analytics.db"
UNCLASSIFIED_JOB_TYPE = "unknown"
OTHER_REGION_KEY = "Others"
MIN_CITY_JOBS    = 10

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


def _build_jobs_where(
    city: str = None, whv_only: bool = False,
    state_filter: str = None, job_types: list[str] = None,
    exclude_urgent: bool = False, source: str = None,
    regional_area: int = None,
) -> tuple[list, dict]:
    clauses, params = ["j.is_deleted = 0", "j.delisted_at IS NULL"], {}
    if city == OTHER_REGION_KEY:
        clauses.append("""j.city IN (
            SELECT city FROM jobs
            WHERE city IS NOT NULL AND is_whv_friendly = 1
              AND is_deleted = 0 AND delisted_at IS NULL
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
    "City of Sydney": "Sydney", "Concord": "Sydney", "Villawood": "Sydney",
    "Milperra": "Sydney", "Parramatta": "Sydney", "Blacktown": "Sydney",
    "Liverpool": "Sydney", "Bankstown": "Sydney", "Fairfield": "Sydney",
    "Campbelltown": "Sydney", "Penrith": "Sydney", "Auburn": "Sydney",
    "Hurstville": "Sydney", "Rockdale": "Sydney", "Sutherland": "Sydney",
    "Cronulla": "Sydney", "Miranda": "Sydney", "Chatswood": "Sydney",
    "Hornsby": "Sydney", "Ryde": "Sydney", "Manly": "Sydney",
    "Bondi": "Sydney", "Randwick": "Sydney", "Marrickville": "Sydney",
    "Burwood": "Sydney", "Strathfield": "Sydney", "Ashfield": "Sydney",
    "Leichhardt": "Sydney", "Newtown": "Sydney", "Redfern": "Sydney",
    "Surry Hills": "Sydney", "Pyrmont": "Sydney", "Rozelle": "Sydney",
    "Balmain": "Sydney", "Drummoyne": "Sydney", "Five Dock": "Sydney",
    "Homebush": "Sydney", "Lidcombe": "Sydney", "Granville": "Sydney",
    "Merrylands": "Sydney", "Guildford": "Sydney", "Wentworthville": "Sydney",
    "Seven Hills": "Sydney", "Toongabbie": "Sydney", "Kings Langley": "Sydney",
    "Wetherill Park": "Sydney", "Smithfield": "Sydney", "Cabramatta": "Sydney",
    "Canley Vale": "Sydney", "Bonnyrigg": "Sydney", "Moorebank": "Sydney",
    "Casula": "Sydney", "Prestons": "Sydney", "Edmondson Park": "Sydney",
    "Ingleburn": "Sydney", "Minto": "Sydney", "Leumeah": "Sydney",
    "Narellan": "Sydney", "Camden": "Sydney", "Macquarie Park": "Sydney",
    "North Ryde": "Sydney", "Meadowbank": "Sydney", "Rhodes": "Sydney",
    "Lane Cove": "Sydney", "Artarmon": "Sydney", "St Leonards": "Sydney",
    "Crows Nest": "Sydney", "North Sydney": "Sydney", "Neutral Bay": "Sydney",
    "Mosman": "Sydney", "Cremorne": "Sydney", "Kirrawee": "Sydney",
    "Engadine": "Sydney", "Menai": "Sydney", "Caringbah": "Sydney",

    # Melbourne metro
    "Derrimut": "Melbourne", "Lilydale": "Melbourne", "Carlton": "Melbourne",
    "Footscray": "Melbourne", "Dandenong": "Melbourne", "Frankston": "Melbourne",
    "Ringwood": "Melbourne", "Box Hill": "Melbourne", "Doncaster": "Melbourne",
    "Werribee": "Melbourne", "Sunshine": "Melbourne", "Broadmeadows": "Melbourne",
    "Thomastown": "Melbourne", "Preston": "Melbourne", "Brunswick": "Melbourne",
    "Fitzroy": "Melbourne", "Richmond": "Melbourne", "St Kilda": "Melbourne",
    "Prahran": "Melbourne", "Oakleigh": "Melbourne", "Clayton": "Melbourne",
    "Springvale": "Melbourne", "Cheltenham": "Melbourne", "Moorabbin": "Melbourne",
    "Bentleigh": "Melbourne", "Mooroolbark": "Melbourne", "Croydon": "Melbourne",
    "Bayswater": "Melbourne", "Ferntree Gully": "Melbourne", "Knox": "Melbourne",
    "Boronia": "Melbourne", "Wantirna": "Melbourne", "Rowville": "Melbourne",
    "Scoresby": "Melbourne", "Knoxfield": "Melbourne", "Narre Warren": "Melbourne",
    "Berwick": "Melbourne", "Cranbourne": "Melbourne", "Pakenham": "Melbourne",
    "Hallam": "Melbourne", "Hampton Park": "Melbourne", "Fountain Gate": "Melbourne",
    "Epping": "Melbourne", "Lalor": "Melbourne", "Mill Park": "Melbourne",
    "South Morang": "Melbourne", "Bundoora": "Melbourne", "Greensborough": "Melbourne",
    "Watsonia": "Melbourne", "Eltham": "Melbourne", "Diamond Creek": "Melbourne",
    "Reservoir": "Melbourne", "Northcote": "Melbourne", "Thornbury": "Melbourne",
    "Coburg": "Melbourne", "Pascoe Vale": "Melbourne", "Glenroy": "Melbourne",
    "Fawkner": "Melbourne", "Campbellfield": "Melbourne", "Somerton": "Melbourne",
    "Tullamarine": "Melbourne", "Airport West": "Melbourne", "Essendon": "Melbourne",
    "Moonee Ponds": "Melbourne", "Ascot Vale": "Melbourne", "Flemington": "Melbourne",
    "Kensington": "Melbourne", "West Melbourne": "Melbourne", "Port Melbourne": "Melbourne",
    "South Melbourne": "Melbourne", "Albert Park": "Melbourne", "Middle Park": "Melbourne",
    "South Yarra": "Melbourne", "Toorak": "Melbourne", "Armadale": "Melbourne",
    "Malvern": "Melbourne", "Glen Waverley": "Melbourne", "Mount Waverley": "Melbourne",
    "Nunawading": "Melbourne", "Mitcham": "Melbourne", "Vermont": "Melbourne",
    "Chirnside Park": "Melbourne", "Healesville": "Melbourne",

    # Brisbane metro
    "South Brisbane": "Brisbane", "Bowen Hills": "Brisbane", "Heathwood": "Brisbane",
    "Underwood": "Brisbane", "Thornlands": "Brisbane", "Cleveland": "Brisbane",
    "Chermside": "Brisbane", "Carindale": "Brisbane", "Indooroopilly": "Brisbane",
    "Toowong": "Brisbane", "Nundah": "Brisbane", "Wynnum": "Brisbane",
    "Stafford": "Brisbane", "Aspley": "Brisbane", "Strathpine": "Brisbane",
    "Springwood": "Brisbane", "Slacks Creek": "Brisbane", "Richlands": "Brisbane",
    "Acacia Ridge": "Brisbane", "Eight Mile Plains": "Brisbane",
    "Fortitude Valley": "Brisbane", "West End": "Brisbane", "New Farm": "Brisbane",
    "Newstead": "Brisbane", "Teneriffe": "Brisbane", "Albion": "Brisbane",
    "Woolloongabba": "Brisbane", "Greenslopes": "Brisbane", "Holland Park": "Brisbane",
    "Mount Gravatt": "Brisbane", "Mansfield": "Brisbane", "Rochedale": "Brisbane",
    "Sunnybank": "Brisbane", "Moorooka": "Brisbane", "Yeronga": "Brisbane",
    "Rocklea": "Brisbane", "Archerfield": "Brisbane", "Coopers Plains": "Brisbane",
    "Salisbury": "Brisbane", "Nathan": "Brisbane", "Tarragindi": "Brisbane",
    "Annerley": "Brisbane", "Fairfield": "Brisbane", "Yeronga": "Brisbane",
    "Chelmer": "Brisbane", "Sherwood": "Brisbane", "Graceville": "Brisbane",
    "Corinda": "Brisbane", "Oxley": "Brisbane", "Darra": "Brisbane",
    "Wacol": "Brisbane", "Inala": "Brisbane", "Forest Lake": "Brisbane",
    "Durack": "Brisbane", "Doolandella": "Brisbane", "Pallara": "Brisbane",
    "Willawong": "Brisbane", "Larapinta": "Brisbane", "Algester": "Brisbane",
    "Calamvale": "Brisbane", "Parkinson": "Brisbane", "Robertson": "Brisbane",
    "Runcorn": "Brisbane", "Kuraby": "Brisbane", "Drewvale": "Brisbane",
    "Stretton": "Brisbane", "Macgregor": "Brisbane", "Upper Mount Gravatt": "Brisbane",
    "Wishart": "Brisbane", "Mackenzie": "Brisbane", "Belmont": "Brisbane",
    "Tingalpa": "Brisbane", "Hemmant": "Brisbane", "Lytton": "Brisbane",
    "Pinkenba": "Brisbane", "Eagle Farm": "Brisbane", "Northgate": "Brisbane",
    "Banyo": "Brisbane", "Nudgee": "Brisbane", "Geebung": "Brisbane",
    "Zillmere": "Brisbane", "Boondall": "Brisbane", "Taigum": "Brisbane",
    "Fitzgibbon": "Brisbane", "Bracken Ridge": "Brisbane", "Bald Hills": "Brisbane",
    "Sandgate": "Brisbane", "Brighton": "Brisbane", "Deagon": "Brisbane",
    "Shorncliffe": "Brisbane", "Clontarf": "Brisbane", "Redcliffe": "Brisbane",
    "Woody Point": "Brisbane", "Scarborough": "Brisbane",

    # Gold Coast
    "Nerang": "Gold Coast", "Southport": "Gold Coast", "Surfers Paradise": "Gold Coast",
    "Broadbeach": "Gold Coast", "Robina": "Gold Coast", "Coomera": "Gold Coast",
    "Helensvale": "Gold Coast", "Labrador": "Gold Coast", "Runaway Bay": "Gold Coast",
    "Bundall": "Gold Coast", "Ashmore": "Gold Coast", "Molendinar": "Gold Coast",
    "Arundel": "Gold Coast", "Parkwood": "Gold Coast", "Carrara": "Gold Coast",
    "Merrimac": "Gold Coast", "Varsity Lakes": "Gold Coast", "Mudgeeraba": "Gold Coast",
    "Tallai": "Gold Coast", "Worongary": "Gold Coast", "Highland Park": "Gold Coast",
    "Bonogin": "Gold Coast", "Mudgeeraba": "Gold Coast", "Reedy Creek": "Gold Coast",
    "Burleigh Heads": "Gold Coast", "Burleigh Waters": "Gold Coast",
    "Elanora": "Gold Coast", "Palm Beach": "Gold Coast", "Currumbin": "Gold Coast",
    "Tugun": "Gold Coast", "Coolangatta": "Gold Coast", "Bilinga": "Gold Coast",
    "Tweed Heads": "Gold Coast",

    # Perth metro
    "Kenwick": "Perth", "Joondalup": "Perth", "Fremantle": "Perth",
    "Rockingham": "Perth", "Midland": "Perth", "Cannington": "Perth",
    "Morley": "Perth", "Osborne Park": "Perth", "Malaga": "Perth",
    "Balcatta": "Perth", "Victoria Park": "Perth", "Subiaco": "Perth",
    "Claremont": "Perth", "Cottesloe": "Perth", "Stirling": "Perth",
    "Innaloo": "Perth", "Karrinyup": "Perth", "Scarborough": "Perth",
    "Nollamara": "Perth", "Westminster": "Perth", "Mirrabooka": "Perth",
    "Girrawheen": "Perth", "Koondoola": "Perth", "Wanneroo": "Perth",
    "Wangara": "Perth", "Landsdale": "Perth", "Darch": "Perth",
    "Madeley": "Perth", "Hocking": "Perth", "Quinns Rocks": "Perth",
    "Clarkson": "Perth", "Butler": "Perth", "Yanchep": "Perth",
    "Kwinana": "Perth", "Baldivis": "Perth", "Mandurah": "Perth",
    "Spearwood": "Perth", "Cockburn Central": "Perth", "Success": "Perth",
    "Bibra Lake": "Perth", "Yangebup": "Perth", "Hamilton Hill": "Perth",
    "Henderson": "Perth", "Coogee": "Perth", "Munster": "Perth",
    "O'Connor": "Perth", "Beaconsfield": "Perth", "White Gum Valley": "Perth",
    "Hilton": "Perth", "Palmyra": "Perth", "Melville": "Perth",
    "Willetton": "Perth", "Murdoch": "Perth", "Bull Creek": "Perth",
    "Leeming": "Perth", "Winthrop": "Perth", "Kardinya": "Perth",
    "Booragoon": "Perth", "Applecross": "Perth", "Mount Pleasant": "Perth",
    "Ardross": "Perth", "Dalkeith": "Perth", "Nedlands": "Perth",
    "Crawley": "Perth", "Shenton Park": "Perth", "Floreat": "Perth",
    "City Beach": "Perth", "Wembley": "Perth", "Joondanna": "Perth",
    "Mount Hawthorn": "Perth", "Leederville": "Perth", "North Perth": "Perth",
    "Mount Lawley": "Perth", "Inglewood": "Perth", "Bedford": "Perth",
    "Dianella": "Perth", "Embleton": "Perth", "Bayswater": "Perth",
    "Bassendean": "Perth", "Guildford": "Perth", "Swan Valley": "Perth",
    "High Wycombe": "Perth", "Forrestfield": "Perth", "Kalamunda": "Perth",
    "Gosnells": "Perth", "Maddington": "Perth", "Thornlie": "Perth",
    "Southern River": "Perth", "Canning Vale": "Perth", "Harrisdale": "Perth",
    "Piara Waters": "Perth", "Atwell": "Perth", "Aubin Grove": "Perth",
    "Hammond Park": "Perth", "Banjup": "Perth", "Jandakot": "Perth",

    # Canberra
    "Manuka": "Canberra", "Barton": "Canberra", "Deakin": "Canberra",
    "Fyshwick": "Canberra", "Woden": "Canberra", "Belconnen": "Canberra",
    "Tuggeranong": "Canberra", "Gungahlin": "Canberra", "Braddon": "Canberra",
    "Civic": "Canberra", "Bruce": "Canberra", "Phillip": "Canberra",

    # Adelaide metro
    "Para Vista": "Adelaide",
    "Elizabeth": "Adelaide", "Salisbury": "Adelaide", "Tea Tree Gully": "Adelaide",
    "Modbury": "Adelaide", "Glenelg": "Adelaide", "Marion": "Adelaide",
    "Noarlunga": "Adelaide", "Morphett Vale": "Adelaide", "Christies Beach": "Adelaide",
    "Reynella": "Adelaide", "Hackham": "Adelaide", "Onkaparinga Hills": "Adelaide",
    "Mawson Lakes": "Adelaide", "Parafield": "Adelaide", "Salisbury East": "Adelaide",
    "Ingle Farm": "Adelaide", "Para Hills": "Adelaide", "Pooraka": "Adelaide",
    "Gepps Cross": "Adelaide", "Wingfield": "Adelaide", "Mansfield Park": "Adelaide",
    "Kilburn": "Adelaide", "Enfield": "Adelaide", "Blair Athol": "Adelaide",
    "Northfield": "Adelaide", "Valley View": "Adelaide", "Ridgehaven": "Adelaide",
    "Hope Valley": "Adelaide", "Banksia Park": "Adelaide", "Golden Grove": "Adelaide",
    "Greenwith": "Adelaide", "One Tree Hill": "Adelaide",

    # Cairns
    "Woree": "Cairns", "Earlville": "Cairns", "Manunda": "Cairns",
    "Westcourt": "Cairns", "Parramatta Park": "Cairns", "Cairns North": "Cairns",
    "Portsmith": "Cairns", "Stratford": "Cairns", "Smithfield": "Cairns",
    "Caravonica": "Cairns", "Freshwater": "Cairns", "Redlynch": "Cairns",
    "Brinsmead": "Cairns", "Kewarra Beach": "Cairns", "Trinity Beach": "Cairns",
    "Trinity Park": "Cairns", "Yorkeys Knob": "Cairns",
    "Cairns City": "Cairns", "Edge Hill": "Cairns", "Bungalow": "Cairns",
    "Edmonton": "Cairns", "Bentley Park": "Cairns", "Manoora": "Cairns",
    "Mount Sheridan": "Cairns", "Gordonvale": "Cairns", "Whitfield": "Cairns",
    "Palm Cove": "Cairns", "Atherton": "Cairns",

    # Central Coast (NSW)
    "Tuggerah": "Central Coast", "Gosford": "Central Coast", "Wyong": "Central Coast",
    "Terrigal": "Central Coast", "The Entrance": "Central Coast",
    "Bateau Bay": "Central Coast", "Erina": "Central Coast",
    "Mingara": "Central Coast", "Toukley": "Central Coast",
    "Lake Haven": "Central Coast",

    # Newcastle (NSW)
    "Charlestown": "Newcastle", "Kotara": "Newcastle", "Gateshead": "Newcastle",
    "Jesmond": "Newcastle", "Wallsend": "Newcastle", "Broadmeadow": "Newcastle",
    "Mayfield": "Newcastle", "Hamilton": "Newcastle", "Waratah": "Newcastle",
    "Adamstown": "Newcastle", "Merewether": "Newcastle", "Islington": "Newcastle",

    # Townsville metro
    "Aitkenvale": "Townsville", "Kirwan": "Townsville", "South Townsville": "Townsville",
    "Pimlico": "Townsville", "Thuringowa Central": "Townsville", "Mundingburra": "Townsville",
    "Bohle": "Townsville", "Idalia": "Townsville", "Deeragun": "Townsville",
    "Douglas": "Townsville", "Garbutt": "Townsville", "Townsville City": "Townsville",
    "Nelly Bay": "Townsville",

    # Darwin metro (NT)
    "Casuarina": "Darwin", "Winnellie": "Darwin", "Fannie Bay": "Darwin",
    "Parap": "Darwin", "Rapid Creek": "Darwin", "Stuart Park": "Darwin",
    "Tiwi": "Darwin", "Palmerston": "Darwin", "Holtze": "Darwin",
    "Berrimah": "Darwin", "Bakewell": "Darwin", "Coonawarra": "Darwin",
    "Litchfield": "Darwin", "Berrimah": "Darwin",

    # Broome
    "Cable Beach": "Broome", "Djugun": "Broome",

    # Sydney metro (additional)
    "Sydney CBD": "Sydney",

    # Sunshine Coast
    "Maroochydore": "Sunshine Coast", "Caloundra": "Sunshine Coast",
    "Noosa Heads": "Sunshine Coast", "Kawana Waters": "Sunshine Coast",
    "Nambour": "Sunshine Coast", "Buderim": "Sunshine Coast",
    "Sippy Downs": "Sunshine Coast", "Mooloolaba": "Sunshine Coast",
    "Kawana": "Sunshine Coast", "Bokarina": "Sunshine Coast",
    "Birtinya": "Sunshine Coast", "Warana": "Sunshine Coast",
    "Currimundi": "Sunshine Coast", "Aroha": "Sunshine Coast",
    "Mountain Creek": "Sunshine Coast", "Kuluin": "Sunshine Coast",
    "Kunda Park": "Sunshine Coast", "Chevallum": "Sunshine Coast",
}


def backfill_jora_city() -> int:
    """Re-derive and normalise city from location text for active Jora jobs.

    Strips state abbreviation and postcode, then maps known suburbs to their
    parent major city (e.g. "Villawood NSW 2163" → "Sydney").
    """
    import re
    _state_re = re.compile(r'\s+[A-Z]{2,3}(?:\s+\d{4})?$')

    def _parse(location: str | None) -> str | None:
        if not location:
            return None
        suburb = _state_re.sub('', location.strip()).strip()
        if not suburb:
            return None
        return _SUBURB_TO_CITY.get(suburb, suburb)

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
