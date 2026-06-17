"""
dashboard.py — WHV Job Tracker 本機操作面板

啟動: python dashboard.py
瀏覽器: http://localhost:5050
"""

import logging
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# ── Shared log state (single-user local tool — no need for a queue/DB) ────────

_buf_lock  = threading.Lock()
_log_lines: list[str] = []
_is_running = False
_is_done    = False
_is_error   = False


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _buf_lock:
        _log_lines.append(line)
    print(line, file=sys.stderr)


class _BufHandler(logging.Handler):
    """Tees selected logger output into the dashboard log buffer."""
    _SKIP = {"werkzeug", "apscheduler"}

    def emit(self, record: logging.LogRecord) -> None:
        root = record.name.split(".")[0]
        if root in self._SKIP or record.levelno < logging.INFO:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        with _buf_lock:
            _log_lines.append(f"[{ts}] [{record.name}] {record.getMessage()}")


_buf_handler = _BufHandler()
_buf_handler.setFormatter(logging.Formatter("%(message)s"))


# ── Pre-filter regex (mirrors main.py) ────────────────────────────────────────

_TITLE_BLOCK = re.compile(
    r'\bsenior\b|\bchef\b|\bfarmer\b|\bfarming\b|\bfarm\s+manager\b'
    r'|\bfarm\s+hand\b|\bfarm\s+worker\b|\bfarm\s+assistant\b'
    r'|\bagricultural\b|\bagriculture\b',
    re.IGNORECASE,
)
_LOCATION_BLOCK = re.compile(r'\bsydney\b', re.IGNORECASE)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def _fetch_and_classify(config: dict) -> None:
    """Fetch from all sources, store, then classify with Gemini."""
    from sources import adzuna, backpacker_job_board, jora
    from classifier import GeminiNetworkError, classify_batch
    import storage

    all_jobs: list[dict] = []

    _log("  → 抓取 Adzuna...")
    try:
        jobs_a = adzuna.fetch(config)
        _log(f"  ✅ Adzuna: {len(jobs_a)} 筆")
        all_jobs += jobs_a
    except Exception as exc:
        _log(f"  ⚠  Adzuna 失敗: {exc}")

    if config.get("backpacker_job_board", {}).get("enabled", True):
        _log("  → 抓取 Backpacker Job Board...")
        try:
            jobs_b = backpacker_job_board.fetch(config)
            _log(f"  ✅ Backpacker Job Board: {len(jobs_b)} 筆")
            all_jobs += jobs_b
        except Exception as exc:
            _log(f"  ⚠  Backpacker Job Board 失敗: {exc}")

    if config.get("jora_scraping", False):
        _log("  → 抓取 Jora...")
        try:
            jobs_j = jora.fetch(config)
            _log(f"  ✅ Jora: {len(jobs_j)} 筆")
            all_jobs += jobs_j
        except Exception as exc:
            _log(f"  ⚠  Jora 失敗: {exc}")

    kept = [
        j for j in all_jobs
        if not _TITLE_BLOCK.search(j.get("title", ""))
        and not _LOCATION_BLOCK.search(j.get("location") or j.get("city") or "")
    ]
    dropped = len(all_jobs) - len(kept)
    if dropped:
        _log(f"  前置過濾排除 {dropped} 筆（職稱/地點）")
    all_jobs = kept

    if not all_jobs:
        _log("  ⚠  沒有抓到任何職缺，中止抓取流程")
        return

    inserted = storage.upsert_jobs(all_jobs)
    _log(f"  ✅ 儲存完成：{inserted} 筆新增，共 {len(all_jobs)} 筆")

    unclassified_ids = storage.get_unclassified_job_ids()
    to_classify = [j for j in all_jobs if j["id"] in unclassified_ids]

    if to_classify:
        _log(f"  → AI 分類 {len(to_classify)} 筆（可能需要幾分鐘）...")
        try:
            classified, failed = classify_batch(
                config,
                to_classify,
                on_result=lambda jid, r: storage.update_classifier(jid, r),
            )
            _log(f"  ✅ 分類完成：{classified} 成功，{failed} 失敗")
        except GeminiNetworkError as exc:
            _log(f"  ⚠  Gemini 網路錯誤，分類跳過: {exc}")
        except Exception as exc:
            _log(f"  ⚠  分類失敗: {exc}")
    else:
        _log("  所有職缺已分類，跳過分類步驟")

    deleted = storage.soft_delete_non_whv_jobs()
    if deleted:
        _log(f"  軟刪除 {deleted} 筆非 WHV 職缺")


def _pipeline(recipient_email: str) -> None:
    global _is_running, _is_done, _is_error

    root_logger = logging.getLogger()
    root_logger.addHandler(_buf_handler)
    if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)

    try:
        from config import load_config
        import storage
        from analyze import run_analysis
        from report import generate_static_report
        from notifier import send_report_email

        config = load_config()
        storage.init_db()

        # ── Step 1: Check today ────────────────────────────────────────────
        _log("📋 檢查今天是否已有抓取記錄...")
        if storage.fetched_today():
            _log("✅ 今天已有資料，跳過抓取")
        else:
            _log("⏳ 今天尚未抓取，開始完整流程...")
            _fetch_and_classify(config)

        # ── Step 2: Analysis snapshot ──────────────────────────────────────
        _log("📊 產生分析快照...")
        try:
            run_analysis()
            _log("✅ 分析快照完成")
        except Exception as exc:
            _log(f"⚠  分析失敗（不影響報告）: {exc}")

        # ── Step 3: Get today's jobs ───────────────────────────────────────
        _log("🔍 讀取今天的 WHV 職缺清單...")
        jobs = storage.get_todays_whv_jobs()
        _log(f"✅ 共 {len(jobs)} 筆 WHV 友善職缺")

        if not jobs:
            _log("⚠  今天沒有 WHV 職缺資料（可能抓取失敗或全部被過濾）")

        # ── Step 4: Generate HTML report ───────────────────────────────────
        _log("📄 產生靜態 HTML 報告...")
        html = generate_static_report(jobs)
        _log(f"✅ 報告產生完成（{len(html):,} bytes）")

        # ── Step 5: Send email ─────────────────────────────────────────────
        _log(f"📧 寄送報告至 {recipient_email}...")
        send_report_email(config, recipient_email, html)
        _log("✅ 寄信成功！")
        _log("════════ 全部完成 ════════")

        with _buf_lock:
            _is_done = True

    except Exception as exc:
        import traceback
        _log(f"❌ 流程失敗：{exc}")
        _log(traceback.format_exc())
        with _buf_lock:
            _is_done  = True
            _is_error = True

    finally:
        root_logger.removeHandler(_buf_handler)
        with _buf_lock:
            _is_running = False


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(_HTML)


@app.route("/send-report", methods=["POST"])
def send_report():
    global _log_lines, _is_running, _is_done, _is_error

    with _buf_lock:
        if _is_running:
            return jsonify({"error": "流程執行中，請稍候再試"}), 409

    body  = request.get_json(force=True) or {}
    email = (body.get("email") or "").strip()
    if "@" not in email or len(email) < 5:
        return jsonify({"error": "請輸入有效的 email 地址（含 @）"}), 400

    with _buf_lock:
        _log_lines.clear()
        _is_running = True
        _is_done    = False
        _is_error   = False

    threading.Thread(target=_pipeline, args=(email,), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/status")
def status():
    with _buf_lock:
        return jsonify({
            "logs":  list(_log_lines),
            "done":  _is_done,
            "error": _is_error,
        })


# ── Embedded HTML page ────────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WHV Tracker — 操作面板</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,Arial,sans-serif;color:#222;background:#f0f4f8;min-height:100vh}
.hdr{background:#1a73e8;color:#fff;padding:20px 32px}
.hdr h1{font-size:1.25rem}
.hdr p{opacity:.75;font-size:.85rem;margin-top:4px}
.main{max-width:740px;margin:28px auto;padding:0 16px}
.card{background:#fff;border-radius:10px;padding:22px 24px;
      box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:18px}
.card h2{font-size:.95rem;color:#444;margin-bottom:14px;font-weight:600}
.row{display:flex;gap:10px;align-items:stretch}
input[type=email]{
  flex:1;padding:10px 14px;font-size:1rem;
  border:1px solid #ddd;border-radius:7px;outline:none}
input[type=email]:focus{border-color:#1a73e8;box-shadow:0 0 0 3px #e8f0fe}
#btn{
  padding:10px 26px;background:#1a73e8;color:#fff;
  border:none;border-radius:7px;font-size:1rem;cursor:pointer;white-space:nowrap}
#btn:hover:not(:disabled){background:#1557b0}
#btn:disabled{background:#aaa;cursor:not-allowed}
#log{
  background:#0d1117;color:#a8ff78;font-family:'Courier New',monospace;
  font-size:.82rem;line-height:1.65;padding:16px 18px;
  border-radius:8px;height:440px;overflow-y:auto;
  white-space:pre-wrap;word-break:break-all;border:2px solid transparent;
  transition:border-color .3s}
#log.done {border-color:#34a853}
#log.error{border-color:#ea4335;color:#ff6b6b}
#badge{
  display:inline-block;margin-top:10px;padding:4px 13px;
  border-radius:12px;font-size:.8rem;font-weight:600;background:#f0f4f8;color:#666}
#badge.run {background:#fff3cd;color:#856404}
#badge.done{background:#d4edda;color:#155724}
#badge.err {background:#f8d7da;color:#721c24}
</style>
</head>
<body>
<div class="hdr">
  <h1>&#x1F998; WHV Tracker — 操作面板</h1>
  <p>手動觸發：抓取 → 分類 → 分析 → 產生報告 → 寄信</p>
</div>
<div class="main">
  <div class="card">
    <h2>收件 Email</h2>
    <div class="row">
      <input type="email" id="email" placeholder="your@email.com">
      <button id="btn" onclick="start()">發信</button>
    </div>
  </div>
  <div class="card">
    <h2>執行進度 <span id="badge">待機</span></h2>
    <div id="log">點擊「發信」開始執行...</div>
  </div>
</div>
<script>
var poll=null;

async function start(){
  var em=document.getElementById('email').value.trim();
  if(!em.includes('@')){alert('請輸入包含 @ 的 email 地址');return;}
  var btn=document.getElementById('btn');
  var log=document.getElementById('log');
  var badge=document.getElementById('badge');
  btn.disabled=true;
  log.textContent='';
  log.className='';
  badge.textContent='執行中...';
  badge.className='run';
  if(poll)clearInterval(poll);
  try{
    var r=await fetch('/send-report',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:em})
    });
    var d=await r.json();
    if(r.ok&&d.status==='started'){
      poll=setInterval(check,2000);
    }else{
      log.textContent='錯誤：'+(d.error||'未知錯誤');
      log.className='error';
      badge.textContent='失敗';badge.className='err';
      btn.disabled=false;
    }
  }catch(e){
    log.textContent='連線錯誤：'+e;
    log.className='error';
    btn.disabled=false;
  }
}

async function check(){
  try{
    var r=await fetch('/status');
    var d=await r.json();
    var log=document.getElementById('log');
    var badge=document.getElementById('badge');
    if(d.logs&&d.logs.length){
      log.textContent=d.logs.join('\\n');
      log.scrollTop=log.scrollHeight;
    }
    if(d.done){
      clearInterval(poll);
      document.getElementById('btn').disabled=false;
      if(d.error){
        log.className='error';
        badge.textContent='❌ 失敗';badge.className='err';
      }else{
        log.className='done';
        badge.textContent='✅ 完成';badge.className='done';
      }
    }
  }catch(e){console.warn('status check:',e);}
}
</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    port = int(os.environ.get("DASHBOARD_PORT", "5050"))
    print(f"WHV Tracker Dashboard → http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
