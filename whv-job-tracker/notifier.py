import smtplib
import sqlite3
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ANALYTICS_DB = Path(__file__).parent / "analytics.db"


def _load_summary() -> dict | None:
    """從 analytics.db 讀取最新一筆快照的摘要。"""
    if not ANALYTICS_DB.exists():
        return None
    try:
        conn = sqlite3.connect(ANALYTICS_DB)
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT run_id, run_at, total_jobs FROM analysis_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if not run:
            conn.close()
            return None

        cities = conn.execute("""
            SELECT city, SUM(count) AS total
            FROM region_stats WHERE run_id = ?
            GROUP BY city ORDER BY total DESC
        """, (run["run_id"],)).fetchall()

        conn.close()
        return {
            "run_at":     run["run_at"],
            "total_jobs": run["total_jobs"],
            "cities":     [{"city": r["city"], "total": r["total"]} for r in cities],
        }
    except Exception as exc:
        print(f"[notifier] could not read analytics.db: {exc}", file=sys.stderr)
        return None


def _source_error_html(errors: list[str] | None) -> str:
    if not errors:
        return ""
    lines = "".join(
        f"<p style='margin:4px 0;color:#b45309;font-size:0.8rem'>{e}</p>"
        for e in errors
    )
    return f"<div style='margin-top:16px;padding:10px 14px;background:#fef3e2;border-radius:6px'>{lines}</div>"


def _build_html(user_name: str, summary: dict, analysis_url: str,
                source_errors: list[str] | None = None) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    city_rows = "".join(
        f"<tr>"
        f"<td style='padding:7px 12px'>{r['city']}</td>"
        f"<td style='padding:7px 12px;text-align:right;font-weight:600'>{r['total']} 筆</td>"
        f"</tr>"
        for r in summary["cities"]
    )

    return f"""<!DOCTYPE html>
<html>
<body style='font-family:-apple-system,Arial,sans-serif;color:#222;
             max-width:520px;margin:auto;padding:28px 20px'>

  <div style='background:#1a73e8;color:#fff;border-radius:10px 10px 0 0;padding:20px 24px'>
    <h2 style='margin:0;font-size:1.2rem'>🦘 WHV Job Tracker</h2>
  </div>

  <div style='background:#fff;border:1px solid #e0e0e0;border-top:none;
              border-radius:0 0 10px 10px;padding:24px'>

    <p style='font-size:1rem;margin:0 0 6px'>Hi {user_name}，</p>
    <p style='font-size:1.4rem;font-weight:700;color:#1a73e8;margin:0 0 20px'>
      今天的職缺搜尋已更新 ✅
    </p>

    <table style='width:100%;border-collapse:collapse;font-size:0.9rem;margin-bottom:20px'>
      <tr style='background:#f0f4f8'>
        <th style='padding:7px 12px;text-align:left;font-weight:600;border-radius:6px 0 0 0'>城市</th>
        <th style='padding:7px 12px;text-align:right;font-weight:600;border-radius:0 6px 0 0'>職缺數</th>
      </tr>
      {city_rows}
      <tr style='border-top:2px solid #e0e0e0'>
        <td style='padding:8px 12px;font-weight:700'>合計</td>
        <td style='padding:8px 12px;text-align:right;font-weight:700'>{summary['total_jobs']} 筆</td>
      </tr>
    </table>

    <a href='{analysis_url}'
       style='display:inline-block;background:#1a73e8;color:#fff;
              text-decoration:none;padding:10px 22px;border-radius:8px;
              font-size:0.9rem;font-weight:600'>
      查看圓餅圖分析 →
    </a>

    {_source_error_html(source_errors)}
    <p style='margin-top:24px;color:#aaa;font-size:0.75rem'>
      {now_str} · WHV Job Tracker
    </p>
  </div>

</body>
</html>"""


def _build_text(user_name: str, summary: dict) -> str:
    lines = [
        "WHV Job Tracker",
        f"Hi {user_name}，今天的職缺搜尋已更新。",
        "",
        "各城市職缺數：",
    ]
    for r in summary["cities"]:
        lines.append(f"  {r['city']}: {r['total']} 筆")
    lines += ["", f"合計：{summary['total_jobs']} 筆"]
    return "\n".join(lines)


def _build_report_email(
    today: str,
    pages_url: str,
    push_ok: bool,
    today_count: int,
    today_cities: list[str],
    pdf_bytes: bytes | None,
) -> tuple[str, str, str]:
    """Return (subject, plain_text, html_body) for the link-based report email."""
    subject    = f"【WHV Job Tracker】今日澳洲打工度假職缺更新 {today}"
    cities_str = "、".join(today_cities) if today_cities else "—"

    push_warn_text = (
        "\n\n⚠️ 注意：連結可能尚未更新，請稍後重試。" if not push_ok else ""
    )
    push_warn_html = (
        '<p style="color:#b45309;background:#fef3c7;padding:8px 12px;'
        'border-radius:6px;margin:12px 0">⚠️ 注意：連結可能尚未更新，請稍後重試。</p>'
        if not push_ok else ""
    )
    pdf_note_text = (
        "\n\n附件為各城市職缺類型圓餅圖 PDF。" if pdf_bytes is not None else ""
    )

    plain = (
        f"Hi，\n\n"
        f"今天為你整理了最新的澳洲打工度假（WHV）職缺，以下是今日新增的職缺報告：\n\n"
        f"👉 點此查看今日職缺：{pages_url}{push_warn_text}\n\n"
        f"本次摘要：\n"
        f"- 今日新增職缺：{today_count} 筆\n"
        f"- 涵蓋城市：{cities_str}\n"
        f"- 資料來源：Adzuna / Backpacker Job Board / Jora\n\n"
        f"報告包含職缺標題、公司、地點、職缺類型，以及是否提供住宿、\n"
        f"是否急聘、是否位於 WHV 偏遠地區等資訊，可直接在頁面上篩選。\n\n"
        f"如有任何問題歡迎回覆此信。{pdf_note_text}\n\n"
        f"WHV Job Tracker"
    )

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,Arial,sans-serif;color:#222;
             max-width:520px;margin:auto;padding:28px 20px">
  <div style="background:#1a73e8;color:#fff;border-radius:10px 10px 0 0;padding:20px 24px">
    <h2 style="margin:0;font-size:1.2rem">🦘 WHV Job Tracker</h2>
  </div>
  <div style="background:#fff;border:1px solid #e0e0e0;border-top:none;
              border-radius:0 0 10px 10px;padding:24px">
    <p style="margin:0 0 12px">Hi，</p>
    <p style="margin:0 0 16px">
      今天為你整理了最新的澳洲打工度假（WHV）職缺，以下是今日新增的職缺報告：
    </p>
    <p style="margin:0 0 8px">
      <a href="{pages_url}"
         style="display:inline-block;background:#1a73e8;color:#fff;text-decoration:none;
                padding:12px 24px;border-radius:8px;font-size:1rem;font-weight:600">
        👉 點此查看今日職缺
      </a>
    </p>
    {push_warn_html}
    <ul style="line-height:2;margin-top:16px;padding-left:20px">
      <li>今日新增職缺：<strong>{today_count} 筆</strong></li>
      <li>涵蓋城市：{cities_str}</li>
      <li>資料來源：Adzuna / Backpacker Job Board / Jora</li>
    </ul>
    <p style="color:#555;font-size:0.9rem;margin-top:8px">
      報告包含職缺標題、公司、地點、職缺類型，以及是否提供住宿、<br>
      是否急聘、是否位於 WHV 偏遠地區等資訊，可直接在頁面上篩選。
    </p>
    <p style="color:#555;font-size:0.9rem">如有任何問題歡迎回覆此信。</p>
    <p style="margin-top:24px;color:#aaa;font-size:0.75rem">WHV Job Tracker · {today}</p>
  </div>
</body>
</html>"""

    return subject, plain, html


def send_report_email(
    config: dict,
    recipient_email: str,
    html_content: str = "",
    pdf_bytes: bytes | None = None,
    pages_url: str | None = None,
    push_ok: bool = True,
    today_count: int = 0,
    today_cities: list[str] | None = None,
) -> None:
    """Send the daily WHV job report email.

    When pages_url is provided the email contains a clickable GitHub Pages link
    (link-based mode).  pdf_bytes, if present, is still attached as a secondary
    file for scope=all runs.

    Fallback: if pages_url is empty/None the old HTML-attachment behaviour is
    used so existing callers without Pages support keep working.
    """
    from email.mime.application import MIMEApplication

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if pages_url:
        subject, plain, html_body = _build_report_email(
            today, pages_url, push_ok,
            today_count, today_cities or [], pdf_bytes,
        )
        if pdf_bytes is not None:
            msg = MIMEMultipart("mixed")
            msg["Subject"] = subject
            msg["From"]    = config["email"]["sender"]
            msg["To"]      = recipient_email
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(plain,     "plain", "utf-8"))
            alt.attach(MIMEText(html_body, "html",  "utf-8"))
            msg.attach(alt)
            pdf_fname = f"whv-city-distribution-{today}.pdf"
            pdf_att   = MIMEApplication(pdf_bytes, Name=pdf_fname)
            pdf_att["Content-Disposition"] = f'attachment; filename="{pdf_fname}"'
            msg.attach(pdf_att)
        else:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = config["email"]["sender"]
            msg["To"]      = recipient_email
            msg.attach(MIMEText(plain,     "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html",  "utf-8"))
    else:
        # Legacy fallback: attach HTML file directly
        fname   = f"whv-report-{today}.html"
        subject = f"[WHV Tracker] 今日職缺報告 — {today}"
        body    = (
            f"今日 WHV 職缺報告已產生。\n"
            f"請開啟附件 {fname} 查看互動式職缺清單（支援城市、類型篩選）。"
        )
        if pdf_bytes is not None:
            pdf_fname  = f"whv-city-distribution-{today}.pdf"
            body += f"\n附件 {pdf_fname} 為各城市職缺類型圓餅圖。"
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = config["email"]["sender"]
        msg["To"]      = recipient_email
        msg.attach(MIMEText(body, "plain", "utf-8"))
        att = MIMEApplication(html_content.encode("utf-8"), Name=fname)
        att["Content-Disposition"] = f'attachment; filename="{fname}"'
        msg.attach(att)
        if pdf_bytes is not None:
            pdf_att = MIMEApplication(pdf_bytes, Name=pdf_fname)
            pdf_att["Content-Disposition"] = f'attachment; filename="{pdf_fname}"'
            msg.attach(pdf_att)

    smtp_server  = config["email"]["smtp_server"]
    smtp_port    = int(config["email"]["smtp_port"])
    sender       = config["email"]["sender"]
    app_password = config["email"]["app_password"]

    with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(sender, app_password)
        server.sendmail(sender, [recipient_email], msg.as_string())
    print(f"[notifier] report sent to {recipient_email}", file=sys.stderr)


def send_failure_alert(config: dict, error_summary: str) -> None:
    """寄出排程失敗通知給 admin_email（或 email.recipients）。"""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"[WHV Tracker] 排程失敗 — {now_str}"
    body = (
        f"WHV Job Tracker 排程執行失敗。\n\n"
        f"時間：{now_str}\n"
        f"錯誤摘要：{error_summary}\n\n"
        "請檢查 DNS／網路／API 設定，並手動執行 getdata.bat 重試。"
    )

    recipients = config.get("admin_email") or config["email"]["recipients"]
    if isinstance(recipients, str):
        recipients = [recipients]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config["email"]["sender"]
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    smtp_server  = config["email"]["smtp_server"]
    smtp_port    = int(config["email"]["smtp_port"])
    sender       = config["email"]["sender"]
    app_password = config["email"]["app_password"]

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, app_password)
            server.sendmail(sender, recipients, msg.as_string())
        print(f"[notifier] failure alert sent to {recipients}", file=sys.stderr)
    except Exception as exc:
        print(f"[notifier] failed to send failure alert: {exc}", file=sys.stderr)
        raise


def send_update_notification(config: dict, analysis_url: str = "http://localhost:5000/analysis",
                             source_errors: list[str] | None = None):
    """分析快照完成後，寄出簡潔的『已更新』通知信。"""
    summary = _load_summary()
    if not summary:
        print("[notifier] no analytics data — skipping email", file=sys.stderr)
        return

    user_name = config.get("user_name", "there")
    now_str   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject   = f"[WHV Tracker] 今日職缺已更新 — {now_str}"

    html_body = _build_html(user_name, summary, analysis_url, source_errors)
    text_body = _build_text(user_name, summary)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config["email"]["sender"]
    msg["To"]      = ", ".join(config["email"]["recipients"])
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    smtp_server  = config["email"]["smtp_server"]
    smtp_port    = int(config["email"]["smtp_port"])
    sender       = config["email"]["sender"]
    app_password = config["email"]["app_password"]
    recipients   = config["email"]["recipients"]

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, app_password)
            server.sendmail(sender, recipients, msg.as_string())
        print(f"[notifier] update notification sent to {recipients}", file=sys.stderr)
    except Exception as exc:
        print(f"[notifier] failed to send email: {exc}", file=sys.stderr)
        raise
