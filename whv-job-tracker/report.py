"""report.py — generate a self-contained, interactive HTML job report."""

import json
from datetime import date

_LABELS = {
    "hospitality": "餐飲/服務",
    "hotel":       "飯店/住宿",
    "office":      "行政/辦公",
    "retail":      "零售",
    "farm":        "農場",
    "other":       "其他",
    "unknown":     "未分類",
}

_CSS = """\
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,Arial,sans-serif;color:#222;background:#f0f4f8}
.hdr{background:#1a73e8;color:#fff;padding:20px 32px}
.hdr h1{font-size:1.25rem}
.hdr p{opacity:.8;font-size:.88rem;margin-top:4px}
.wrap{max-width:980px;margin:24px auto;padding:0 14px}
.bar{background:#fff;border-radius:8px;padding:14px 18px;margin-bottom:14px;
     display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap;
     box-shadow:0 1px 4px rgba(0,0,0,.08)}
.bar label{font-size:.8rem;color:#666;display:block;margin-bottom:3px}
select{padding:6px 10px;border:1px solid #ddd;border-radius:5px;font-size:.88rem;background:#fafafa}
#badge{margin-left:auto;background:#e8f0fe;color:#1a73e8;
       padding:3px 12px;border-radius:12px;font-size:.82rem;font-weight:700;align-self:center}
.card{background:#fff;border-radius:8px;padding:14px 18px;margin-bottom:9px;
      box-shadow:0 1px 3px rgba(0,0,0,.07);border-left:4px solid #1a73e8}
.card.urg{border-left-color:#ea4335}
.ttl{font-size:.97rem;font-weight:600}
.ttl a{color:#1a73e8;text-decoration:none}
.ttl a:hover{text-decoration:underline}
.meta{font-size:.8rem;color:#666;margin-top:5px;display:flex;gap:9px;flex-wrap:wrap}
.tag{background:#f0f4f8;border-radius:4px;padding:2px 7px}
.sal{color:#34a853;font-weight:600}
.note{font-size:.77rem;color:#999;margin-top:4px;font-style:italic}
#none{text-align:center;padding:40px;color:#aaa;display:none}
</style>"""

# JS uses __JOBS__ and __LABELS__ as placeholders replaced via str.replace().
_JS = """\
<script>
var J=__JOBS__;
var L=__LABELS__;
function sal(a,b){
  if(!a&&!b)return'';
  if(a&&b)return'$'+Math.round(a)+'–$'+Math.round(b)+'/yr';
  if(a)return'$'+Math.round(a)+'+ /yr';
  return'up to $'+Math.round(b)+'/yr';
}
function render(){
  var city=document.getElementById('fc').value;
  var type=document.getElementById('ft').value;
  var urg=document.getElementById('fu').value;
  var f=J.filter(function(j){
    if(city&&j.city!==city)return false;
    if(type&&j.job_type!==type)return false;
    if(urg&&j.urgency!==urg)return false;
    return true;
  });
  document.getElementById('badge').textContent=f.length+' 筆';
  var list=document.getElementById('list');
  if(!f.length){list.innerHTML='';document.getElementById('none').style.display='';return;}
  document.getElementById('none').style.display='none';
  list.innerHTML=f.map(function(j){
    var s=sal(j.salary_min,j.salary_max);
    var tl=L[j.job_type]||j.job_type;
    var reg=j.regional_area===1?'🌿 偏遠':'';
    var uu=j.urgency==='high'?'🔴 緊急':'';
    var th=j.url?'<a href="'+j.url+'" target="_blank">'+j.title+'</a>':j.title;
    return'<div class="card'+(j.urgency==='high'?' urg':'')+'">'+
      '<div class="ttl">'+th+'</div>'+
      '<div class="meta">'+
        '<span>'+(j.company||'—')+'</span>'+
        '<span>'+(j.location||j.city)+'</span>'+
        (s?'<span class="sal">'+s+'</span>':'')+
        '<span class="tag">'+tl+'</span>'+
        '<span class="tag">'+j.source+'</span>'+
        (reg?'<span class="tag">'+reg+'</span>':'')+
        (uu?'<span class="tag">'+uu+'</span>':'')+
      '</div>'+
      (j.classifier_note?'<div class="note">'+j.classifier_note+'</div>':'')+
    '</div>';
  }).join('');
}
render();
</script>"""


_PIE_COLORS = {
    "hospitality": "#4A90D9",
    "hotel":       "#E8637A",
    "retail":      "#A78BDB",
    "farm":        "#F7D154",
    "office":      "#F4A742",
    "other":       "#5BC8A0",
}
_PIE_DEFAULT_COLOR = "#B0B0B0"


def generate_pie_chart_report(conn) -> bytes:
    """Generate a PDF of per-city job_type pie charts.
    Cities are read dynamically from the DB — no hardcoded list."""
    import io
    import math
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    rows = conn.execute("""
        SELECT city, job_type, COUNT(*) AS cnt
        FROM jobs
        WHERE delisted_at IS NULL AND is_deleted = 0
        GROUP BY city, job_type
        ORDER BY city
    """).fetchall()

    city_data: dict[str, dict[str, int]] = {}
    for r in rows:
        city = r[0] or "Unknown"
        jt   = r[1] or "other"
        cnt  = r[2]
        if city not in city_data:
            city_data[city] = {}
        city_data[city][jt] = city_data[city].get(jt, 0) + cnt

    # Sort by total count descending
    cities = sorted(city_data.keys(), key=lambda c: -sum(city_data[c].values()))

    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        if not cities:
            fig, ax = plt.subplots(figsize=(8, 4))
            ax.text(0.5, 0.5, "No job data available", ha="center", va="center", fontsize=14)
            ax.axis("off")
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)
            return buf.getvalue()

        COLS           = 2
        ROWS_PER_PAGE  = 3
        charts_per_page = COLS * ROWS_PER_PAGE

        for page_start in range(0, len(cities), charts_per_page):
            batch  = cities[page_start : page_start + charts_per_page]
            n_rows = math.ceil(len(batch) / COLS)
            fig, axs = plt.subplots(n_rows, COLS, figsize=(14, 5 * n_rows), squeeze=False)
            fig.suptitle(
                "WHV Job Tracker — Job Type Distribution by City",
                fontsize=13, fontweight="bold",
            )
            axes_flat = [axs[r][c] for r in range(n_rows) for c in range(COLS)]

            for i, city in enumerate(batch):
                ax     = axes_flat[i]
                d      = city_data[city]
                keys   = list(d.keys())
                labels = [k.capitalize() for k in keys]
                values = list(d.values())
                colors = [_PIE_COLORS.get(k, _PIE_DEFAULT_COLOR) for k in keys]
                total  = sum(values)
                ax.pie(
                    values, labels=labels, colors=colors,
                    autopct="%1.1f%%", startangle=90, pctdistance=0.80,
                )
                ax.set_title(f"{city}  ({total} jobs)", fontsize=11, fontweight="bold", pad=14)

            for i in range(len(batch), len(axes_flat)):
                axes_flat[i].set_visible(False)

            plt.tight_layout(rect=[0, 0, 1, 0.95])
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)

    return buf.getvalue()


def generate_static_report(jobs: list[dict]) -> str:
    today = date.today().strftime("%Y-%m-%d")
    total = len(jobs)

    safe = [
        {
            "id":              j.get("id") or "",
            "title":           j.get("title") or "",
            "company":         j.get("company") or "",
            "location":        j.get("location") or "",
            "city":            j.get("city") or "",
            "job_type":        j.get("job_type") or "unknown",
            "salary_min":      j.get("salary_min"),
            "salary_max":      j.get("salary_max"),
            "url":             j.get("url") or "",
            "source":          j.get("source") or "",
            "urgency":         j.get("urgency") or "normal",
            "regional_area":   j.get("regional_area"),
            "classifier_note": j.get("classifier_note") or "",
        }
        for j in jobs
    ]

    cities    = sorted({j["city"]     for j in safe if j["city"]})
    job_types = sorted({j["job_type"] for j in safe})

    city_opts = "\n".join(f'<option value="{c}">{c}</option>' for c in cities)
    type_opts = "\n".join(
        f'<option value="{t}">{_LABELS.get(t, t)}</option>' for t in job_types
    )

    jobs_json   = json.dumps(safe, ensure_ascii=False)
    labels_json = json.dumps({k: v for k, v in _LABELS.items()}, ensure_ascii=False)
    js_block    = _JS.replace("__JOBS__", jobs_json).replace("__LABELS__", labels_json)

    return f"""\
<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<title>WHV 職缺報告 — {today}</title>
{_CSS}
</head>
<body>
<div class="hdr">
  <h1>&#x1F998; WHV 職缺報告</h1>
  <p>{today} &nbsp;·&nbsp; 共 {total} 筆 WHV 友善職缺</p>
</div>
<div class="wrap">
  <div class="bar">
    <div><label>城市</label>
      <select id="fc" onchange="render()">
        <option value="">全部城市</option>
        {city_opts}
      </select>
    </div>
    <div><label>職缺類型</label>
      <select id="ft" onchange="render()">
        <option value="">全部類型</option>
        {type_opts}
      </select>
    </div>
    <div><label>緊急程度</label>
      <select id="fu" onchange="render()">
        <option value="">全部</option>
        <option value="normal">一般</option>
        <option value="low">不急</option>
        <option value="high">緊急</option>
      </select>
    </div>
    <span id="badge"></span>
  </div>
  <div id="list"></div>
  <div id="none">沒有符合條件的職缺</div>
</div>
{js_block}
</body>
</html>"""
