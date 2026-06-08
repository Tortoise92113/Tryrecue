"""
analyze.py
讀取 jobs.db，計算各地區 x 工作類別的統計數據，寫入 analytics.db。
每次執行都會新增一筆 analysis_run 記錄，可追蹤歷史趨勢。
"""

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from storage import ANALYTICS_DB, DB_PATH as JOBS_DB

LABEL_MAP = {
    "hospitality": "餐飲/服務",
    "hotel": "飯店/住宿",
    "office": "行政/辦公",
    "retail": "零售",
    "other": "其他",
    None: "未分類",
}


def init_analytics_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS analysis_runs (
            run_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at   TEXT NOT NULL,
            total_jobs INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS region_stats (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id     INTEGER NOT NULL REFERENCES analysis_runs(run_id),
            city       TEXT NOT NULL,
            job_type   TEXT NOT NULL,
            count      INTEGER NOT NULL,
            percentage REAL NOT NULL
        );
    """)
    conn.commit()


def fetch_cross_table(jobs_conn: sqlite3.Connection) -> tuple[list[tuple], int]:
    cur = jobs_conn.cursor()
    cur.execute("""
        SELECT city, job_type, COUNT(*) as count
        FROM jobs
        WHERE city IS NOT NULL
        GROUP BY city, job_type
        ORDER BY city, count DESC
    """)
    rows = cur.fetchall()

    cur.execute("SELECT COUNT(*) FROM jobs WHERE city IS NOT NULL")
    total = cur.fetchone()[0]
    return rows, total


def compute_stats(rows: list[tuple]) -> tuple[dict, dict]:
    """回傳 {city: {job_type: count}} 以及每城市總數。"""
    data: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    city_totals: dict[str, int] = defaultdict(int)

    for city, job_type, count in rows:
        jtype = job_type if job_type else "unknown"
        data[city][jtype] += count
        city_totals[city] += count

    return data, city_totals


def run_analysis() -> None:
    jobs_conn = sqlite3.connect(JOBS_DB)
    analytics_conn = sqlite3.connect(ANALYTICS_DB)
    init_analytics_db(analytics_conn)

    rows, total = fetch_cross_table(jobs_conn)
    jobs_conn.close()

    data, city_totals = compute_stats(rows)
    run_at = datetime.now(timezone.utc).isoformat()

    cur = analytics_conn.cursor()
    cur.execute(
        "INSERT INTO analysis_runs (run_at, total_jobs) VALUES (?, ?)",
        (run_at, total),
    )
    run_id = cur.lastrowid

    stat_rows = []
    for city, type_counts in data.items():
        city_total = city_totals[city]
        for jtype, count in type_counts.items():
            pct = round(count / city_total * 100, 2) if city_total else 0.0
            stat_rows.append((run_id, city, jtype, count, pct))
    cur.executemany(
        "INSERT INTO region_stats (run_id, city, job_type, count, percentage) VALUES (?,?,?,?,?)",
        stat_rows,
    )

    analytics_conn.commit()
    analytics_conn.close()

    # --- 終端機摘要 ---
    print(f"分析完成  run_id={run_id}  時間={run_at}")
    print(f"總筆數: {total}  涵蓋城市: {len(data)}")
    print()

    cities_sorted = sorted(city_totals.keys(), key=lambda c: -city_totals[c])
    for city in cities_sorted:
        city_total = city_totals[city]
        print(f"[{city}] {city_total} 筆")
        for jtype, count in sorted(data[city].items(), key=lambda x: -x[1]):
            pct = count / city_total * 100
            bar = "█" * int(pct / 4)
            label = LABEL_MAP.get(jtype, jtype)
            print(f"  {label:<10} {count:>3} ({pct:5.1f}%) {bar}")
        print()


if __name__ == "__main__":
    run_analysis()
