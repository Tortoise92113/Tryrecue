import os
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, render_template, request

import storage
from config import load_config
from filters import pre_filter
from storage import ANALYTICS_DB, UNCLASSIFIED_JOB_TYPE

_config_cache: dict | None = None


def _get_config() -> dict:
    global _config_cache
    if _config_cache is None:
        try:
            _config_cache = load_config()
        except Exception as exc:
            from flask import abort
            app.logger.error("Config load failed: %s", exc)
            abort(500, description=f"Configuration error: {exc}")
    return _config_cache


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


# ── Auto-shutdown when the browser is closed (opt-in via AUTO_SHUTDOWN=1) ───────
# Open pages send a heartbeat every few seconds; if none arrive for a grace
# period (i.e. the last tab was closed), the server shuts itself down. The grace
# period is longer than the ping interval so a page refresh / navigation does
# not trip it. Disabled by default so `python app.py` dev runs are unaffected.
_AUTO_SHUTDOWN = os.environ.get("AUTO_SHUTDOWN") == "1"
_HEARTBEAT_INTERVAL = 3      # seconds between browser pings (foreground)
# Fast path: when a tab fires `pagehide` it sends an "unload" beacon. The server
# waits this grace period; any heartbeat in the meantime (a navigation's new page,
# or another still-open tab) cancels the shutdown. So closing the browser stops
# the server in ~_CLOSE_GRACE seconds, while page navigation / multi-tab survive.
_CLOSE_GRACE = 8
# Backstop: if the unload beacon never fires (crash, hard close) and no heartbeat
# arrives for this long, shut down anyway. Well above the ~60s background-tab
# timer throttle so an idle/backgrounded tab is never killed by mistake.
_HEARTBEAT_TIMEOUT = 120
_last_beat = {"t": None}     # None until the first heartbeat arrives
_closing = {"t": None}       # set when a tab unloads; cleared by any heartbeat

# Beat on load + interval, immediately on regaining visibility/focus, and send an
# unload beacon on pagehide so a real browser close shuts the server down fast.
_HEARTBEAT_SNIPPET = (
    "<script>(function(){"
    "function b(){fetch('/api/heartbeat',{method:'POST',keepalive:true}).catch(function(){});}"
    "b();setInterval(b,%d);"
    "document.addEventListener('visibilitychange',function(){if(!document.hidden)b();});"
    "window.addEventListener('focus',b);"
    "window.addEventListener('pagehide',function(){try{navigator.sendBeacon('/api/unload');}catch(e){}});"
    "})();</script>" % (_HEARTBEAT_INTERVAL * 1000)
)


@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    _last_beat["t"] = datetime.now().timestamp()
    _closing["t"] = None          # a live page → cancel any pending close
    return ("", 204)


@app.route("/api/unload", methods=["POST"])
def unload():
    _closing["t"] = datetime.now().timestamp()
    return ("", 204)


@app.after_request
def _inject_heartbeat(resp):
    if _AUTO_SHUTDOWN and (resp.content_type or "").startswith("text/html"):
        try:
            body = resp.get_data(as_text=True)
            if "</body>" in body:
                resp.set_data(body.replace("</body>", _HEARTBEAT_SNIPPET + "</body>", 1))
        except Exception:
            pass
    return resp


def _shutdown_watchdog():
    while True:
        threading.Event().wait(2)
        last = _last_beat["t"]
        # Only arm after the first heartbeat; never kill a running pipeline.
        if last is None or _dash_running:
            continue
        now = datetime.now().timestamp()
        closing = _closing["t"]
        # Fast path: a tab unloaded and nothing has beaten since (real close).
        if closing is not None and now - closing > _CLOSE_GRACE:
            os._exit(0)
        # Backstop: no heartbeat at all for a long time.
        if now - last > _HEARTBEAT_TIMEOUT:
            os._exit(0)


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
    deleted, skipped = storage.permanent_delete_jobs()
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
        if job_type == UNCLASSIFIED_JOB_TYPE:
            rows = conn.execute("""
                SELECT id, title, company, location, salary_min, salary_max,
                       url, is_whv_friendly, regional_area, source, posted_at,
                       is_favorite
                FROM jobs
                WHERE city = ? AND job_type IS NULL
                  AND is_deleted = 0 AND delisted_at IS NULL
                ORDER BY posted_at DESC
            """, (city,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, title, company, location, salary_min, salary_max,
                       url, is_whv_friendly, regional_area, source, posted_at,
                       is_favorite
                FROM jobs
                WHERE city = ? AND job_type = ?
                  AND is_deleted = 0 AND delisted_at IS NULL
                ORDER BY posted_at DESC
            """, (city, job_type)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/")
def index():
    config = _get_config()
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
    source       = request.args.get("source") or None
    try:
        ra_str        = request.args.get("regional_area")
        regional_area = int(ra_str) if ra_str is not None else None
    except (ValueError, TypeError):
        regional_area = None
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (ValueError, TypeError):
        offset = 0
    try:
        limit = min(int(request.args.get("limit", 300)), 1000)
    except (ValueError, TypeError):
        return jsonify({"error": "invalid limit"}), 400
    job_types_raw = request.args.get("job_types") or ""
    job_types    = [t.strip() for t in job_types_raw.split(",") if t.strip()] or None

    exclude_urgent = _get_config().get("filters", {}).get("exclude_urgent", False)
    kwargs = dict(city=city, whv_only=whv_only, state_filter=state_filter,
                  job_types=job_types, exclude_urgent=exclude_urgent,
                  source=source, regional_area=regional_area)
    total = storage.count_jobs(**kwargs)
    rows  = storage.get_jobs(**kwargs, limit=limit, offset=offset)
    # grand_total: view-context total (whv/regional/exclude_urgent only, no city/source/etc.)
    # Only computed on first page to avoid redundant queries on load-more
    grand_total = None
    if offset == 0:
        view_kwargs = dict(whv_only=whv_only, regional_area=regional_area,
                           exclude_urgent=exclude_urgent)
        grand_total = storage.count_jobs(**view_kwargs)
    return jsonify({"jobs": [dict(r) for r in rows], "total": total,
                    "grand_total": grand_total})


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
            WHERE is_favorite = 1 AND is_deleted = 0 AND delisted_at IS NULL
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
    exclude_urgent = _get_config().get("filters", {}).get("exclude_urgent", False)
    # Use (? = 0 OR <condition>) so urgency/whv filters can be toggled without
    # building different SQL strings — avoids f-string interpolation entirely.
    urgent_p = 1 if exclude_urgent else 0
    whv_p    = 1 if whv_only else 0
    with storage.get_conn() as conn:
        summary = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN j.is_whv_friendly = 1 THEN 1 ELSE 0 END) AS whv,
                   SUM(CASE WHEN j.regional_area = 1 THEN 1 ELSE 0 END) AS regional
            FROM jobs j
            LEFT JOIN job_user_states s ON j.id = s.job_id
            WHERE j.is_deleted = 0
              AND j.delisted_at IS NULL
              AND COALESCE(s.state, 'new') != 'hidden'
              AND (? = 0 OR (j.urgency IS NULL OR j.urgency != 'high'))
        """, (urgent_p,)).fetchone()
        by_source = conn.execute("""
            SELECT j.source, COUNT(*) AS cnt
            FROM jobs j LEFT JOIN job_user_states s ON j.id = s.job_id
            WHERE j.is_deleted = 0
              AND j.delisted_at IS NULL
              AND COALESCE(s.state, 'new') != 'hidden'
              AND (? = 0 OR (j.urgency IS NULL OR j.urgency != 'high'))
              AND (? = 0 OR j.is_whv_friendly = 1)
            GROUP BY j.source
        """, (urgent_p, whv_p)).fetchall()
        by_state = conn.execute("""
            SELECT COALESCE(s.state,'new') AS state, COUNT(*) AS cnt
            FROM jobs j LEFT JOIN job_user_states s ON j.id = s.job_id
            WHERE j.is_deleted = 0
              AND j.delisted_at IS NULL
              AND (? = 0 OR (j.urgency IS NULL OR j.urgency != 'high'))
            GROUP BY state
        """, (urgent_p,)).fetchall()
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
    # FLASK_DEBUG=0 (set by the background launcher) runs a single clean process
    # with no auto-reloader; default keeps debug on for direct `python app.py` dev.
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    if _AUTO_SHUTDOWN:
        threading.Thread(target=_shutdown_watchdog, daemon=True).start()
    app.run(debug=debug, port=port, use_reloader=debug)
