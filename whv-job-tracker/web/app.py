import os
import sqlite3
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, render_template, request

import storage
from config import load_config

ANALYTICS_DB = Path(__file__).parent.parent / "analytics.db"


def _analytics_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(ANALYTICS_DB)
    conn.row_factory = sqlite3.Row
    # Expose a flat view so queries read naturally as "analysis_snapshots"
    conn.execute("""
        CREATE VIEW IF NOT EXISTS analysis_snapshots AS
        SELECT ar.run_id, ar.run_at, ar.total_jobs,
               rs.city, rs.job_type, rs.count, rs.percentage
        FROM analysis_runs ar
        JOIN region_stats rs ON ar.run_id = rs.run_id
    """)
    return conn


def _latest_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT run_id FROM analysis_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    return row["run_id"] if row else None

app = Flask(__name__)
_db_ready = False


@app.before_request
def ensure_db():
    global _db_ready
    if not _db_ready:
        storage.init_db()
        _db_ready = True


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/favorites")
def favorites_page():
    return render_template("favorites.html")


@app.route("/deleted")
def deleted_page():
    return render_template("deleted.html")


@app.route("/api/jobs/deleted")
def api_deleted_jobs():
    rows = storage.get_deleted_jobs()
    return jsonify([dict(r) for r in rows])


@app.route("/api/jobs/permanent_delete", methods=["POST"])
def api_permanent_delete():
    deleted, skipped = storage.permanent_delete_jobs([])
    return jsonify({"deleted": deleted, "skipped": skipped})


@app.route("/api/jobs/purge", methods=["POST"])
def api_purge_jobs():
    body = request.get_json(force=True) or {}
    job_ids = body.get("job_ids", [])
    purged = storage.purge_jobs(job_ids)
    return jsonify({"purged": purged})


@app.route("/analysis")
def analysis():
    return render_template("analysis.html")


@app.route("/analysis/jobs/<job_type>/<path:city>")
def analysis_jobs_page(job_type: str, city: str):
    return render_template("jobs_list.html", city=city, job_type=job_type)


@app.route("/api/analysis/jobs/<job_type>/<path:city>")
def api_analysis_jobs(job_type: str, city: str):
    with storage.get_conn() as conn:
        rows = conn.execute("""
            SELECT id, title, company, location, salary_min, salary_max,
                   url, is_whv_friendly, regional_area, source, posted_at,
                   is_favorite
            FROM jobs
            WHERE city = ? AND job_type = ?
            ORDER BY posted_at DESC
        """, (city, job_type)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/")
def index():
    config = load_config()
    cities = config["search"]["cities"]
    return render_template("index.html", cities=cities, user_name=config.get("user_name", ""))


# ── Analysis API ──────────────────────────────────────────────────────────────

@app.route("/api/analysis/cities")
def api_analysis_cities():
    """城市清單 + 各城市最新快照總筆數。"""
    if not ANALYTICS_DB.exists():
        return jsonify([])
    conn = _analytics_conn()
    try:
        run_id = _latest_run_id(conn)
        if run_id is None:
            return jsonify([])
        rows = conn.execute("""
            SELECT city, SUM(count) AS total
            FROM analysis_snapshots
            WHERE run_id = ?
            GROUP BY city
            ORDER BY total DESC
        """, (run_id,)).fetchall()
        return jsonify([{"city": r["city"], "total": r["total"]} for r in rows])
    finally:
        conn.close()


@app.route("/api/analysis/chart/<path:city>")
def api_analysis_chart(city: str):
    """指定城市今天最新一筆快照的各 job_type 分佈。"""
    if not ANALYTICS_DB.exists():
        return jsonify([])
    conn = _analytics_conn()
    try:
        run_id = _latest_run_id(conn)
        if run_id is None:
            return jsonify([])
        rows = conn.execute("""
            SELECT job_type, count, percentage
            FROM analysis_snapshots
            WHERE run_id = ? AND city = ?
            ORDER BY count DESC
        """, (run_id, city)).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/analysis/snapshot_date/<path:city>")
def api_analysis_snapshot_date(city: str):
    """該城市最後一次更新時間。"""
    if not ANALYTICS_DB.exists():
        return jsonify({"date": None})
    conn = _analytics_conn()
    try:
        row = conn.execute("""
            SELECT run_at FROM analysis_snapshots
            WHERE city = ?
            ORDER BY run_id DESC
            LIMIT 1
        """, (city,)).fetchone()
        return jsonify({"date": row["run_at"] if row else None})
    finally:
        conn.close()


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/jobs")
def api_jobs():
    city         = request.args.get("city") or None
    whv_only     = request.args.get("whv_only") == "1"
    state_filter = request.args.get("state") or None
    limit        = min(int(request.args.get("limit", 200)), 1000)
    job_types_raw = request.args.get("job_types") or ""
    job_types    = [t.strip() for t in job_types_raw.split(",") if t.strip()] or None

    rows = storage.get_jobs(city=city, whv_only=whv_only,
                            state_filter=state_filter,
                            job_types=job_types, limit=limit)
    return jsonify([dict(r) for r in rows])


@app.route("/api/jobs/<job_id>/soft_delete", methods=["POST"])
def api_soft_delete(job_id):
    storage.soft_delete_job(job_id)
    return jsonify({"success": True})


@app.route("/api/jobs/<job_id>/restore", methods=["POST"])
def api_restore_job(job_id):
    storage.restore_job(job_id)
    return jsonify({"success": True})


@app.route("/api/jobs/favorites")
def api_favorites():
    with storage.get_conn() as conn:
        rows = conn.execute("""
            SELECT id, title, company, location, city, job_type,
                   salary_min, salary_max, url, is_whv_friendly,
                   regional_area, source, favorited_at
            FROM jobs
            WHERE is_favorite = 1 AND is_deleted = 0
            ORDER BY favorited_at DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/jobs/<job_id>/favorite", methods=["POST"])
def api_toggle_favorite(job_id):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with storage.get_conn() as conn:
        row = conn.execute("SELECT is_favorite FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        new_val = 0 if row["is_favorite"] else 1
        conn.execute(
            "UPDATE jobs SET is_favorite = ?, favorited_at = ? WHERE id = ?",
            (new_val, now if new_val else None, job_id),
        )
    return jsonify({"is_favorite": new_val})


@app.route("/api/jobs/<job_id>/state", methods=["POST"])
def api_set_state(job_id):
    body  = request.get_json(force=True) or {}
    state = body.get("state", "new")
    note  = body.get("note")
    if state not in ("new", "saved", "applied", "hidden"):
        return jsonify({"error": "invalid state"}), 400
    storage.set_user_state(job_id, state, note)
    return jsonify({"ok": True})


@app.route("/api/stats")
def api_stats():
    whv_only = request.args.get("whv_only") == "1"
    src_whv  = "AND j.is_whv_friendly = 1" if whv_only else ""
    with storage.get_conn() as conn:
        summary = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN j.is_whv_friendly = 1 THEN 1 ELSE 0 END) AS whv,
                   SUM(CASE WHEN j.regional_area = 1 THEN 1 ELSE 0 END) AS regional
            FROM jobs j
            LEFT JOIN job_user_states s ON j.id = s.job_id
            WHERE j.is_deleted = 0
              AND COALESCE(s.state, 'new') != 'hidden'
        """).fetchone()
        by_source = conn.execute(
            f"SELECT j.source, COUNT(*) AS cnt "
            f"FROM jobs j LEFT JOIN job_user_states s ON j.id = s.job_id "
            f"WHERE j.is_deleted = 0 AND COALESCE(s.state, 'new') != 'hidden' {src_whv} "
            f"GROUP BY j.source"
        ).fetchall()
        by_state = conn.execute(
            "SELECT COALESCE(s.state,'new') AS state, COUNT(*) AS cnt "
            "FROM jobs j LEFT JOIN job_user_states s ON j.id=s.job_id "
            "WHERE j.is_deleted = 0 "
            "GROUP BY state"
        ).fetchall()
    return jsonify({
        "total":        summary["total"],
        "whv_friendly": summary["whv"],
        "regional":     summary["regional"],
        "by_source": {r["source"]: r["cnt"] for r in by_source},
        "by_state":  {r["state"]:  r["cnt"] for r in by_state},
    })


if __name__ == "__main__":
    storage.init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)
