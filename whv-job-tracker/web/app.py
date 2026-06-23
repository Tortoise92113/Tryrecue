import logging
import os
import sqlite3
import sys
import threading
import time
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
_file_log_attached = False


def _ensure_file_logging() -> None:
    """Attach the weekly log file handler to the root logger if not already present.

    run_scheduler.py calls _setup_logging() at startup which adds this handler.
    When Flask is started directly via web/app.py that never runs, so dashboard-
    triggered pipeline runs would skip the log file without this guard.
    """
    global _file_log_attached
    if _file_log_attached:
        return
    root = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        _file_log_attached = True
        return
    try:
        from run_scheduler import _WeeklyFileHandler
        log_dir = Path(__file__).parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh = _WeeklyFileHandler(log_dir)
        fh.setFormatter(fmt)
        root.addHandler(fh)
        _file_log_attached = True
    except Exception as exc:
        print(f"[app] could not attach log file handler: {exc}", file=sys.stderr)


# ── Dashboard shared state ────────────────────────────────────────────────────
_dash_lock     = threading.Lock()
_dash_logs: list[str] = []
_dash_running  = False
_dash_done     = False
_dash_error    = False
_dash_start_at: float | None = None   # wall-clock start time of current pipeline run

PIPELINE_TIMEOUT = 3600  # seconds before watchdog force-kills a hung pipeline


def _dlog(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _dash_lock:
        if len(_dash_logs) >= 2000:
            _dash_logs.pop(0)
        _dash_logs.append(line)
    print(line, file=sys.stderr)


class _DashBufHandler(logging.Handler):
    _SKIP = {"werkzeug", "apscheduler"}

    def emit(self, record: logging.LogRecord) -> None:
        root = record.name.split(".")[0]
        if root in self._SKIP or record.levelno < logging.INFO:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        with _dash_lock:
            _dash_logs.append(f"[{ts}] [{record.name}] {record.getMessage()}")


_dash_buf_handler = _DashBufHandler()


def _dash_fetch_and_classify(config: dict) -> None:
    from sources import adzuna, backpacker_job_board, jora
    from classifier import GeminiNetworkError, classify_batch

    all_jobs: list[dict] = []

    _dlog("  → 抓取 Adzuna...")
    try:
        jobs_a = adzuna.fetch(config)
        _dlog(f"  ✅ Adzuna: {len(jobs_a)} 筆")
        all_jobs += jobs_a
    except Exception as exc:
        _dlog(f"  ⚠  Adzuna 失敗: {exc}")

    if config.get("backpacker_job_board", {}).get("enabled", True):
        _dlog("  → 抓取 Backpacker Job Board...")
        try:
            jobs_b = backpacker_job_board.fetch(config)
            _dlog(f"  ✅ Backpacker Job Board: {len(jobs_b)} 筆")
            all_jobs += jobs_b
        except Exception as exc:
            _dlog(f"  ⚠  Backpacker Job Board 失敗: {exc}")

    if config.get("jora_scraping", False):
        _dlog("  → 抓取 Jora...")
        try:
            jobs_j = jora.fetch(config)
            _dlog(f"  ✅ Jora: {len(jobs_j)} 筆")
            all_jobs += jobs_j
        except Exception as exc:
            _dlog(f"  ⚠  Jora 失敗: {exc}")

    all_jobs, dropped = pre_filter(all_jobs, config)
    if dropped:
        _dlog(f"  前置過濾排除 {dropped} 筆（職稱/地點）")

    if not all_jobs:
        _dlog("  ⚠  沒有抓到任何職缺，中止抓取流程")
        return

    inserted = storage.upsert_jobs(all_jobs)
    _dlog(f"  ✅ 儲存完成：{inserted} 筆新增，共 {len(all_jobs)} 筆")

    presence = storage.update_job_presence({j["id"] for j in all_jobs})
    if presence["revived"] or presence["newly_delisted"]:
        _dlog(f"  📊 自動下架：{presence['newly_delisted']} 筆 | 重新上架：{presence['revived']} 筆")

    unclassified_ids = storage.get_unclassified_job_ids()
    to_classify = [j for j in all_jobs if j["id"] in unclassified_ids]

    if to_classify:
        _dlog(f"  → AI 分類 {len(to_classify)} 筆（可能需要幾分鐘）...")
        try:
            classified, failed = classify_batch(
                config,
                to_classify,
                on_result=lambda jid, r: storage.update_classifier(jid, r),
            )
            _dlog(f"  ✅ 分類完成：{classified} 成功，{failed} 失敗")
        except GeminiNetworkError as exc:
            _dlog(f"  ⚠  Gemini 網路錯誤，分類跳過: {exc}")
        except Exception as exc:
            _dlog(f"  ⚠  分類失敗: {exc}")
    else:
        _dlog("  所有職缺已分類，跳過分類步驟")

    deleted = storage.soft_delete_non_whv_jobs()
    if deleted:
        _dlog(f"  軟刪除 {deleted} 筆非 WHV 職缺")


def _dash_pipeline(recipient_email: str, scope: str = "today") -> None:
    global _dash_running, _dash_done, _dash_error, _dash_start_at

    _ensure_file_logging()
    root_logger = logging.getLogger()
    root_logger.addHandler(_dash_buf_handler)
    if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)

    try:
        from analyze import run_analysis
        from report import generate_static_report, generate_pie_chart_report
        from notifier import send_report_email

        config = _get_config()
        storage.init_db()

        _dlog("📋 檢查今天是否已有抓取記錄...")
        if storage.fetched_today():
            _dlog("✅ 今天已有資料，跳過抓取")
            from datetime import date as _date
            _today = _date.today().isoformat()
            with storage.get_conn() as _conn:
                _src_rows = _conn.execute(
                    "SELECT source, COUNT(*) AS cnt FROM jobs"
                    " WHERE date(fetched_at, 'localtime') = ? AND is_deleted = 0"
                    " GROUP BY source ORDER BY cnt DESC",
                    (_today,),
                ).fetchall()
                _presence = _conn.execute(
                    "SELECT"
                    "  SUM(CASE WHEN date(delisted_at,'localtime')=? THEN 1 ELSE 0 END) AS delisted,"
                    "  SUM(CASE WHEN date(last_seen_at,'localtime')=? AND missed_count=0"
                    "           AND delisted_at IS NULL"
                    "           AND date(fetched_at,'localtime')!=? THEN 1 ELSE 0 END) AS revived"
                    " FROM jobs WHERE is_deleted=0",
                    (_today, _today, _today),
                ).fetchone()
            _parts = "、".join(f"{r['source']} {r['cnt']} 筆" for r in _src_rows)
            _total = sum(r['cnt'] for r in _src_rows)
            _dlog(f"  📊 今日抓取：{_parts}（共 {_total} 筆）")
            _d, _r = _presence["delisted"] or 0, _presence["revived"] or 0
            if _d or _r:
                _dlog(f"  📊 自動下架：{_d} 筆 | 重新上架：{_r} 筆")
            # If Jora didn't run today (e.g. previous crash), fetch it now
            if config.get("jora_scraping", False) and not storage.source_fetched_today("jora"):
                from sources import jora as jora_src
                from filters import pre_filter
                _dlog("  → Jora 今天尚無資料（上次可能中斷），補抓取中...")
                try:
                    jora_jobs = jora_src.fetch(config)
                    if jora_jobs:
                        jora_jobs, dropped = pre_filter(jora_jobs, config)
                        inserted = storage.upsert_jobs(jora_jobs)
                        # Reset missed_count for re-fetched jobs without penalising
                        # other sources (calling update_job_presence would increment
                        # missed_count for Adzuna/BJB jobs not in this partial run).
                        revived = storage.mark_jobs_seen({j["id"] for j in jora_jobs})
                        msg = f"  ✅ Jora 補抓完成：{len(jora_jobs)} 筆（新增 {inserted} 筆）"
                        if revived:
                            msg += f"，復活 {revived} 筆"
                        _dlog(msg)
                    else:
                        _dlog("  ⚠  Jora 補抓完成：0 筆")
                except Exception as exc:
                    _dlog(f"  ⚠  Jora 補抓失敗（不影響其他資料）: {exc}")
            # Classify any jobs that weren't classified (e.g. from a crashed run)
            unclassified = storage.get_unclassified_jobs()
            if unclassified:
                from classifier import GeminiNetworkError, classify_batch
                _dlog(f"  → 發現 {len(unclassified)} 筆未分類職缺，補分類中...")
                try:
                    classified, failed = classify_batch(
                        config, unclassified,
                        on_result=lambda jid, r: storage.update_classifier(jid, r),
                    )
                    _dlog(f"  ✅ 補分類完成：{classified} 成功，{failed} 失敗")
                except GeminiNetworkError as exc:
                    _dlog(f"  ❌ 網路錯誤，無法分類：{exc}")
                except Exception as exc:
                    _dlog(f"  ⚠  分類失敗（不影響報告）: {exc}")
        else:
            _dlog("⏳ 今天尚未抓取，開始完整流程...")
            _dash_fetch_and_classify(config)

        _dlog("📊 產生分析快照...")
        try:
            run_analysis()
            _dlog("✅ 分析快照完成")
        except Exception as exc:
            _dlog(f"⚠  分析失敗（不影響報告）: {exc}")

        _dlog("📄 產生 GitHub Pages 報告並推送...")
        try:
            from pages_publisher import publish_today, PublishResult
            pr: PublishResult = publish_today()
            if pr.push_ok:
                _dlog(f"✅ Pages 推送完成：今日 {pr.today_count} 筆 / 全部 {pr.all_count} 筆")
                _dlog(f"   🔗 今日：{pr.today_url}")
                _dlog(f"   🔗 全部：{pr.all_url}")
            else:
                _dlog(f"⚠️  Pages 推送失敗（連結尚未更新）")
                _dlog(f"   🔗 今日：{pr.today_url}")
                _dlog(f"   🔗 全部：{pr.all_url}")
        except Exception as exc:
            _dlog(f"⚠️  Pages 發布失敗（不影響寄信）: {exc}")
            pr = None

        pdf_bytes = None
        if scope == "all":
            _dlog("📊 產生城市職缺分布圓餅圖 PDF...")
            try:
                with storage.get_conn() as conn:
                    pdf_bytes = generate_pie_chart_report(conn)
                _dlog(f"✅ PDF 產生完成（{len(pdf_bytes):,} bytes）")
            except Exception as exc:
                _dlog(f"⚠  PDF 產生失敗（不影響寄信）: {exc}")

        # scope="all" → full-report link; scope="today" → today-only link
        report_url = (pr.all_url if scope == "all" else pr.today_url) if pr else None

        _dlog(f"📧 寄送報告至 {recipient_email}...")
        send_report_email(
            config, recipient_email,
            pdf_bytes=pdf_bytes,
            pages_url=report_url,
            push_ok=pr.push_ok if pr else False,
            today_count=pr.today_count if pr else 0,
            today_cities=pr.today_cities if pr else [],
        )
        _dlog("✅ 寄信成功！")
        _dlog("════════ 全部完成 ════════")

    except Exception as exc:
        import traceback
        _dlog(f"❌ 流程失敗：{exc}")
        _dlog(traceback.format_exc())
        with _dash_lock:
            _dash_error = True

    finally:
        root_logger.removeHandler(_dash_buf_handler)
        with _dash_lock:
            _dash_running  = False
            _dash_done     = True
            _dash_start_at = None


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
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return ("", 403)
    _closing["t"] = datetime.now().timestamp()
    return ("", 204)


@app.after_request
def _inject_heartbeat(resp):
    if _AUTO_SHUTDOWN and (resp.content_type or "").startswith("text/html"):
        try:
            body = resp.get_data(as_text=True)
            if "</body>" in body:
                # Serving a new HTML page means a live tab is navigating to it —
                # reset the close countdown immediately rather than waiting for the
                # new page's first heartbeat POST (which could take a few seconds).
                _closing["t"] = None
                resp.set_data(body.replace("</body>", _HEARTBEAT_SNIPPET + "</body>", 1))
        except Exception:
            pass
    return resp


def _shutdown_watchdog():
    while True:
        time.sleep(2)
        last = _last_beat["t"]
        # Only arm after the first heartbeat.
        if last is None:
            continue
        with _dash_lock:
            running  = _dash_running
            start_at = _dash_start_at
        if running:
            # Never kill an active pipeline — but force-exit if it has hung past the limit.
            if start_at is not None and time.time() - start_at > PIPELINE_TIMEOUT:
                _dlog("pipeline exceeded timeout, force-exiting")
                os._exit(1)
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


# ── Dashboard routes ──────────────────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/dashboard/send-report", methods=["POST"])
def dashboard_send_report():
    global _dash_running, _dash_done, _dash_error, _dash_start_at

    body  = request.get_json(force=True) or {}
    email = (body.get("email") or "").strip()
    if "@" not in email or len(email) < 5:
        return jsonify({"error": "請輸入有效的 email 地址（含 @）"}), 400
    scope = body.get("scope") or "today"
    if scope not in ("today", "all"):
        scope = "today"

    with _dash_lock:
        if _dash_running:
            return jsonify({"error": "流程執行中，請稍候再試"}), 409
        _dash_logs.clear()
        _dash_running  = True
        _dash_done     = False
        _dash_error    = False
        _dash_start_at = time.time()

    threading.Thread(target=_dash_pipeline, args=(email, scope), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/dashboard/status")
def dashboard_status():
    with _dash_lock:
        return jsonify({
            "logs":  list(_dash_logs),
            "done":  _dash_done,
            "error": _dash_error,
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
