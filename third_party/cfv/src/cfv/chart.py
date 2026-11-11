"""Haftalık değişkenler → tek dosyalık etkileşimli grafik (data/processed/chart.html).

Her değişken kendi maksimumuna göre ölçeklenir (farklı birimler aynı eksende kıyaslanabilsin);
imleç altındaki gerçek değer lejantta görünür. Bir haftaya tıklamak o haftanın klasörünü verir.
Dış bağımlılık yok: uPlot (MIT) dosyanın içine gömülür, sayfa çevrimdışı açılır.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path

from cfv import db

STATIC = Path(__file__).parent / "static"
KIND_LABELS = {
    "photo": "Foto", "video": "Video", "whatsapp": "WhatsApp mesajı", "calendar": "Takvim olayı",
    "todo": "Ödev maddesi", "note": "Not", "email": "E-posta", "sleep": "Uyku kaydı",
    "workout": "Egzersiz sayısı", "zepp_day": "Zepp günü",
}
SKIP_KINDS = {"zepp_day", "sleep", "day_report", "report", "dream"}  # bunlar aşağıda anlamlı birimle var
TOP_PEOPLE = 12


def _monday(d: str) -> str:
    x = dt.date.fromisoformat(d)
    return (x - dt.timedelta(days=x.weekday())).isoformat()


def _week_sql(col: str = "local_date") -> str:
    # SQLite: haftanın pazartesisi
    return f"date({col}, '-' || ((CAST(strftime('%w', {col}) AS INT) + 6) % 7) || ' days')"


def collect(conn) -> dict:
    W = _week_sql()
    series: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)  # grup → etiket → hafta → değer

    def put(group, label, rows):
        d = {w: v for w, v in rows if w and v}
        if d:
            series[group][label] = d

    for kind, in conn.execute("SELECT DISTINCT kind FROM event"):
        if kind in SKIP_KINDS:
            continue
        put("Genel", KIND_LABELS.get(kind, kind),
            conn.execute(f"SELECT {W}, count(*) FROM event WHERE kind=? GROUP BY 1", (kind,)))

    put("Genel", "WhatsApp (gönderdiğim)", conn.execute(
        f"SELECT {W}, count(*) FROM event WHERE kind='whatsapp' AND json_extract(payload,'$.from_me') GROUP BY 1"))
    put("Genel", "Ödev (tamamlanan)", conn.execute(
        f"SELECT {W}, count(*) FROM event WHERE kind='todo' AND json_extract(payload,'$.done') GROUP BY 1"))
    put("Genel", "Takvim (saat)", conn.execute(
        f"""SELECT {W}, round(sum((julianday(end_utc) - julianday(start_utc)) * 24), 1) FROM event
            WHERE kind='calendar' AND time_quality='exact' AND end_utc IS NOT NULL
              AND julianday(end_utc) - julianday(start_utc) < 1 GROUP BY 1"""))
    put("Genel", "Takvim olayında çekilen foto", conn.execute(
        f"""SELECT {_week_sql('p.local_date')}, count(DISTINCT p.id) FROM event_link l
            JOIN event p ON p.id = l.a WHERE l.kind='photo_at' GROUP BY 1"""))

    put("Sağlık", "Adım (günlük ort.)", conn.execute(
        f"SELECT {W}, round(avg(json_extract(payload,'$.steps'))) FROM event WHERE kind='zepp_day' GROUP BY 1"))
    put("Sağlık", "Uyku (saat, gece ort.)", conn.execute(
        f"SELECT {W}, round(avg(json_extract(payload,'$.asleep_min')) / 60.0, 2) FROM event WHERE kind='sleep' GROUP BY 1"))
    put("Sağlık", "Dinlenme nabzı", conn.execute(
        f"SELECT {W}, round(avg(json_extract(payload,'$.rest_hr')), 1) FROM event WHERE kind='zepp_day' GROUP BY 1"))
    put("Sağlık", "Egzersiz (dk)", conn.execute(
        f"""SELECT {W}, round(sum((julianday(end_utc) - julianday(start_utc)) * 1440)) FROM event
            WHERE kind='workout' GROUP BY 1"""))

    # Kişiler: fotoda birlikte görünme ve birebir mesajlaşma (en çok görünen TOP_PEOPLE kişi)
    for group, role, kinds, extra in (
        ("Kişi · fotoda", "in_photo", "('photo','video')", ""),
        ("Kişi · mesaj", "counterpart", "('whatsapp')", "AND json_extract(e.payload,'$.chat_type') = 'dm'"),
        ("Kişi · e-posta", "%", "('email')", ""),
    ):
        top = conn.execute(f"""
            SELECT p.id, p.display_name FROM event_person ep JOIN person p ON p.id = ep.person_id
            JOIN event e ON e.id = ep.event_id
            WHERE ep.role LIKE ? AND e.kind IN {kinds} {extra} AND p.is_me = 0
            GROUP BY p.id ORDER BY count(*) DESC LIMIT {TOP_PEOPLE}""", (role,)).fetchall()
        for pid, name in top:
            put(group, name, conn.execute(f"""
                SELECT {_week_sql('e.local_date')}, count(DISTINCT e.id) FROM event_person ep
                JOIN event e ON e.id = ep.event_id
                WHERE ep.person_id = ? AND ep.role LIKE ? AND e.kind IN {kinds} {extra} GROUP BY 1""",
                (pid, role)))
    return series


def week_dirs(timeline: Path) -> dict[str, list[str]]:
    """'YYYY-MM-DD' (pazartesi) → hafta klasörleri (ay sınırında iki tane olabilir)."""
    out: dict[str, list[str]] = defaultdict(list)
    for p in sorted(timeline.glob("*/*/W*")):
        m = re.match(r"W\d+ \((\d+)-\d+\)", p.name)
        mm = re.match(r"(\d+)-", p.parent.name)
        if not (m and mm and p.parent.parent.name.isdigit()):
            continue
        d = dt.date(int(p.parent.parent.name), int(mm.group(1)), int(m.group(1)))
        out[_monday(d.isoformat())].append(str(p / "README.md"))
    return out


def build(conn, out: Path | None = None) -> dict:
    out = out or db.data_dir() / "processed" / "chart.html"
    series = collect(conn)
    weeks = sorted({w for g in series.values() for s in g.values() for w in s})
    first, last = dt.date.fromisoformat(weeks[0]), dt.date.fromisoformat(weeks[-1])
    today = dt.date.today()
    last = min(last, today - dt.timedelta(days=today.weekday()))
    allw = [(first + dt.timedelta(weeks=i)).isoformat() for i in range((last - first).days // 7 + 1)]
    data = {
        "weeks": allw,
        "groups": [{"name": g, "series": [{"label": k, "values": [v.get(w) for w in allw]}
                                          for k, v in s.items()]} for g, s in series.items()],
        "dirs": week_dirs(db.data_dir() / "processed" / "timeline"),
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    html = TEMPLATE.replace("/*UPLOT_CSS*/", (STATIC / "uPlot.min.css").read_text()) \
                   .replace("/*UPLOT_JS*/", (STATIC / "uPlot.iife.min.js").read_text()) \
                   .replace("/*DATA*/", json.dumps(data, ensure_ascii=False))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    out.chmod(0o600)
    return {"path": str(out), "weeks": len(allw), "series": sum(len(g) for g in series.values())}


TEMPLATE = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CFV zaman çizelgesi</title>
<style>/*UPLOT_CSS*/
:root{--bg:#fafaf8;--fg:#222;--muted:#777;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--fg:#e6e6e6;--muted:#999;--line:#333}}
body{margin:0;font:14px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg);display:flex;height:100vh}
#side{width:270px;overflow:auto;border-right:1px solid var(--line);padding:10px 12px;flex:none}
#main{flex:1;display:flex;flex-direction:column;min-width:0;padding:10px}
#chart{flex:1;min-height:300px}
h1{font-size:16px;margin:0 0 6px} h2{font-size:13px;margin:14px 0 4px;color:var(--muted);display:flex;justify-content:space-between}
h2 a{font-weight:normal;cursor:pointer;text-decoration:underline}
label{display:flex;gap:6px;align-items:center;padding:1px 0;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sw{width:10px;height:10px;border-radius:2px;flex:none}
#info{color:var(--muted);font-size:12px;margin-top:6px;min-height:2.6em}
#info a{color:inherit}
.opts{margin:6px 0;color:var(--muted);font-size:12px}
.u-legend{font-size:12px}.u-legend .u-series{padding:0 6px}
</style></head><body>
<div id="side"><h1>CFV</h1>
<div class="opts"><label><input type="checkbox" id="smooth"> 4 haftalık ortalama</label>
<label><input type="checkbox" id="norm" checked> her çizgiyi kendi maksimumuna ölçekle</label></div>
<div id="groups"></div></div>
<div id="main"><div id="chart"></div><div id="info">Bir haftaya tıkla → o haftanın klasörü.</div></div>
<script>/*UPLOT_JS*/</script>
<script>
const D = /*DATA*/;
const xs = D.weeks.map(w => Date.parse(w + "T00:00:00") / 1000);
const all = [];
D.groups.forEach(g => g.series.forEach(s => all.push({...s, group: g.name})));
all.forEach((s, i) => s.color = `hsl(${(i * 137.5) % 360} 65% 50%)`);
const KEY = "cfv-chart-on2";
all.forEach(s => s.key = s.group + "|" + s.label);
let on;
try { on = new Set(JSON.parse(localStorage.getItem(KEY))); } catch (e) {}
if (!on || !on.size) on = new Set(["Genel|Foto", "Genel|WhatsApp mesajı", "Genel|Ödev maddesi", "Genel|Takvim olayı"]);
const save = () => { try { localStorage.setItem(KEY, JSON.stringify([...on])); } catch (e) {} };

const box = document.getElementById("groups");
D.groups.forEach(g => {
  const h = document.createElement("h2");
  h.innerHTML = `<span>${g.name}</span><a>temizle</a>`;
  h.querySelector("a").onclick = () => { g.series.forEach(s => on.delete(g.name + "|" + s.label)); save(); render(); sync(); };
  box.appendChild(h);
  g.series.forEach(s => {
    const key = g.name + "|" + s.label, c = all.find(a => a.key === key).color;
    const l = document.createElement("label");
    l.innerHTML = `<input type="checkbox"><span class="sw" style="background:${c}"></span><span title="${s.label}">${s.label}</span>`;
    const cb = l.querySelector("input"); cb.dataset.key = key;
    cb.onchange = () => { cb.checked ? on.add(key) : on.delete(key); save(); render(); };
    box.appendChild(l);
  });
});
const sync = () => box.querySelectorAll("input[data-key]").forEach(cb => cb.checked = on.has(cb.dataset.key));
sync();

function smooth(v) {
  return v.map((_, i) => { let s = 0, n = 0; for (let j = Math.max(0, i - 3); j <= i; j++) { s += v[j] || 0; n++; } return +(s / n).toFixed(2); });
}
let plot;
function render() {
  const sel = all.filter(s => on.has(s.key));
  const sm = document.getElementById("smooth").checked, norm = document.getElementById("norm").checked;
  const raw = sel.map(s => sm ? smooth(s.values) : s.values.map(v => v ?? 0));
  const ys = raw.map(v => { const m = Math.max(...v) || 1; return norm ? v.map(x => x / m) : v; });
  const el = document.getElementById("chart");
  plot && plot.destroy();
  plot = new uPlot({
    width: el.clientWidth, height: el.clientHeight - 40,
    cursor: {drag: {x: true, y: false}},
    scales: {x: {time: true}},
    axes: [{stroke: getComputedStyle(document.body).color, grid: {stroke: "#8883"}},
           {stroke: getComputedStyle(document.body).color, grid: {stroke: "#8883"}, show: !norm}],
    series: [{label: "Hafta", value: (u, v) => v == null ? "" : new Date(v * 1000).toLocaleDateString("tr-TR")},
      ...sel.map((s, i) => ({label: s.label, stroke: s.color, width: 1.5, points: {show: false},
        value: (u, v, si, idx) => idx == null ? "" : raw[i][idx]}))],
  }, [xs, ...ys], el);
  plot.over.onclick = () => {
    const i = plot.cursor.idx; if (i == null) return;
    const dirs = D.dirs[D.weeks[i]] || [];
    document.getElementById("info").innerHTML = `<b>${D.weeks[i]} haftası</b> · ` +
      (dirs.length ? dirs.map(p => `<a href="file://${encodeURI(p)}">${p.split("/timeline/")[1]}</a>`).join(" · ") : "klasör yok") +
      `<br>sürükle = yakınlaş, çift tık = sıfırla · üretildi ${D.generated}`;
  };
}
document.getElementById("smooth").onchange = render;
document.getElementById("norm").onchange = render;
addEventListener("resize", render);
render();
</script></body></html>
"""
