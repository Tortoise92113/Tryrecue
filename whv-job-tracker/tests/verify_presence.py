"""
verify_presence.py — 驗證 update_job_presence 的下架 / 復活邏輯。

使用臨時 SQLite DB，完全不碰正式 jobs.db。
直接執行：python tests/verify_presence.py
"""
import sys
import tempfile
from pathlib import Path

# 讓 import 找得到 storage
sys.path.insert(0, str(Path(__file__).parent.parent))

import storage


def _patch_db(tmp_path: str):
    """把 storage.DB_PATH 暫時指向測試用 DB。"""
    original = storage.DB_PATH
    storage.DB_PATH = Path(tmp_path)
    return original


def _restore_db(original):
    storage.DB_PATH = original


def _make_job(id_: str, city: str = "Brisbane") -> dict:
    return {
        "id": id_,
        "source": "test",
        "title": f"Job {id_}",
        "company": "TestCo",
        "location": city,
        "city": city,
        "description": "",
        "url": f"https://example.com/{id_}",
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "is_whv_friendly": 1,
    }


def _row(conn, job_id: str):
    return conn.execute(
        "SELECT missed_count, delisted_at, last_seen_at FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()


def run():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    original_db = _patch_db(tmp_path)
    try:
        storage.init_db()
        print(f"[TEST DB] {tmp_path}")

        # ── 初始資料：3 個職缺 ────────────────────────────────────────────
        jobs = [_make_job("A"), _make_job("B"), _make_job("C")]
        storage.upsert_jobs(jobs)
        print("[Run 0] inserted A B C")

        # ── Run 1：A B C 全部出現 → missed_count 維持 0 ──────────────────
        p1 = storage.update_job_presence({"A", "B", "C"})
        with storage.get_conn() as conn:
            for jid in ["A", "B", "C"]:
                r = _row(conn, jid)
                assert r["missed_count"] == 0, f"{jid}: missed_count={r['missed_count']} (expected 0)"
                assert r["delisted_at"] is None, f"{jid}: should not be delisted"
        print(f"[Run 1] A B C all seen: revived={p1['revived']} newly_delisted={p1['newly_delisted']} OK")

        # Run 2: only A seen -> B C missed_count = 1
        p2 = storage.update_job_presence({"A"})
        with storage.get_conn() as conn:
            rA = _row(conn, "A"); rB = _row(conn, "B"); rC = _row(conn, "C")
            assert rA["missed_count"] == 0
            assert rB["missed_count"] == 1, f"B: {rB['missed_count']} (expected 1)"
            assert rC["missed_count"] == 1, f"C: {rC['missed_count']} (expected 1)"
            assert rB["delisted_at"] is None, "B: still not delisted (only 1 miss)"
            assert rC["delisted_at"] is None, "C: still not delisted (only 1 miss)"
        print(f"[Run 2] only A: B.missed=1 C.missed=1 OK")

        # Run 3: only A again -> B C missed_count = 2, NOT yet delisted (threshold is 3)
        p3 = storage.update_job_presence({"A"})
        with storage.get_conn() as conn:
            rB = _row(conn, "B"); rC = _row(conn, "C")
            assert rB["missed_count"] == 2, f"B: {rB['missed_count']} (expected 2)"
            assert rB["delisted_at"] is None, "B: should NOT be delisted yet (need 3 misses)"
            assert rC["delisted_at"] is None, "C: should NOT be delisted yet (need 3 misses)"
        print(f"[Run 3] only A: B.missed=2 C.missed=2 (not delisted yet) OK")
        assert p3["newly_delisted"] == 0, f"newly_delisted={p3['newly_delisted']} (expected 0)"

        # Run 4: only A again -> B C missed_count = 3 -> delisted
        p4 = storage.update_job_presence({"A"})
        with storage.get_conn() as conn:
            rB = _row(conn, "B"); rC = _row(conn, "C")
            assert rB["missed_count"] == 3, f"B: {rB['missed_count']} (expected 3)"
            assert rB["delisted_at"] is not None, "B: should be delisted after 3 misses"
            assert rC["delisted_at"] is not None, "C: should be delisted after 3 misses"
        print(f"[Run 4] only A: B C delisted OK  newly_delisted={p4['newly_delisted']}")
        assert p4["newly_delisted"] == 2, f"newly_delisted={p4['newly_delisted']} (expected 2)"

        # Run 5: B reappears -> revived
        p5 = storage.update_job_presence({"A", "B"})
        with storage.get_conn() as conn:
            rB = _row(conn, "B"); rC = _row(conn, "C")
            assert rB["delisted_at"] is None, "B: should be revived"
            assert rB["missed_count"] == 0, f"B: missed_count={rB['missed_count']} after revival"
            assert rC["delisted_at"] is not None, "C: should remain delisted"
        print(f"[Run 5] A+B seen: B revived OK  revived={p5['revived']}")
        assert p5["revived"] == 1, f"revived={p5['revived']} (expected 1)"

        print("\nAll checks passed. Production jobs.db was not touched.")

    finally:
        _restore_db(original_db)
        Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    run()
