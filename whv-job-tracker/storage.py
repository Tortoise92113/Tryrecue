import sqlite3
import contextlib
from pathlib import Path

DB_PATH      = Path(__file__).parent / "jobs.db"
ANALYTICS_DB = Path(__file__).parent / "analytics.db"

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
    is_purged       INTEGER DEFAULT 0
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
            posted_at, fetched_at,
            is_whv_friendly, visa_sponsorship, accommodation,
            urgency, classifier_note, job_type, regional_area
        ) VALUES (
            :id, :source, :title, :company, :location, :city,
            :description, :url, :salary_min, :salary_max,
            :posted_at, :fetched_at,
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
    rows = [{**defaults, **j} for j in jobs]
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
            "SELECT id FROM jobs WHERE is_whv_friendly IS NULL OR is_whv_friendly = -1"
        ).fetchall()
    return {row["id"] for row in rows}


def get_unclassified_jobs() -> list[dict]:
    """Return full records for jobs not yet classified or that failed classification."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE is_whv_friendly IS NULL OR is_whv_friendly = -1"
        ).fetchall()
    return [dict(r) for r in rows]


def soft_delete_non_whv_jobs() -> int:
    """Soft-delete non-WHV-friendly jobs so they aren't re-classified when re-scraped."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        return conn.execute(
            "UPDATE jobs SET is_deleted = 1, deleted_at = ? "
            "WHERE is_whv_friendly = 0 AND is_deleted = 0",
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


def get_jobs(city: str = None, whv_only: bool = False,
             state_filter: str = None, job_types: list[str] = None,
             exclude_urgent: bool = False,
             limit: int = 200) -> list[sqlite3.Row]:
    clauses, params = ["j.is_deleted = 0"], {}
    if city:
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
    clauses.append("COALESCE(s.state, 'new') != 'hidden'")
    where = " AND ".join(clauses)
    sql = f"""
        SELECT j.*, COALESCE(s.state, 'new') AS user_state, s.note AS user_note
        FROM jobs j
        LEFT JOIN job_user_states s ON j.id = s.job_id
        WHERE {where}
        ORDER BY j.fetched_at DESC
        LIMIT :limit
    """
    params["limit"] = limit
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
          AND COALESCE(s.state, 'new') = 'new'
        ORDER BY j.fetched_at DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, {"since": since_iso}).fetchall()


if __name__ == "__main__":
    init_db()
    print(f"Database initialised at {DB_PATH}")
