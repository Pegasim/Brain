"""t gününü yaz: "olası günü hayal et, benzer günleri hatırla".

1. Hayal: LLM (Haiku ajanı, jobs.py), t'den önceki mini-raporlar + sayılar + known(t) ile t'nin K olası raporunu yazar.
2. Hatırla: her rapor yerel embedding'le t'den önceki mini-raporlara gömülür; en benzer A geçmiş gün
   (son HORIZON gün içinde) + son RECENT gün = örnek havuzu (epizodik hafıza).
3. Her havuz günü bir olası gündür (world model: tek nokta değil dağılım): sayılar o günün gerçek
   değerleri; takvimde known(t) + son günlerin aynı gün eklenen olayları. Tahmin: medyan, kümelerde oy.
LLM sayı yazmaz (denendi: kaba yuvarlak değerler, rakiplerin gerisinde).

Hayal: <ad>/imagined/<gün>.md; örnekler: <ad>/samples/<gün>.json = {"reports": [...], "analogs": [[gün...]...]}
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from common import cfv_db, out_dir, ro
from common.baselines import dow4, yesterday
from common.targets import COUNTS, known

from . import memo, textscore

DOW = ["", "Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
DAY = (Path(__file__).parent / "prompts" / "day.md").read_text()
SHORT = {"msgs_sent": "gönd", "msgs_recv": "gelen", "sessions": "oturum", "sessions_me": "oturum_ben",
         "photos": "foto", "screenshots": "ekran", "todo_done": "ödev_yap", "todo_open": "ödev_açık",
         "cal_events": "takvim", "cal_hours": "takv_saat", "steps": "adım", "sleep_min": "uyku_dk"}
ANALOGS, RECENT, HORIZON, VOTE = 7, 7, 120, 0.4


def _prev(day: str, n: int) -> str:
    return (date.fromisoformat(day) - timedelta(n)).isoformat()


def _dt(utc: str) -> datetime:
    return datetime.fromisoformat(utc.replace("Z", "+00:00"))


def calendar(k: dict) -> str:
    def hm(u):
        return (_dt(u) + timedelta(hours=3)).strftime("%H:%M")
    rows = [f"- tüm gün: {c['title']}" if c["all_day"] or not c["end_utc"]
            else f"- {hm(c['start_utc'])}-{hm(c['end_utc'])} {c['title']}" for c in k["calendar"]]
    return "\n".join(rows) or "- (boş)"


def known_cal(k: dict) -> tuple[int, float]:
    """Önceden bilinen saatli olay sayısı ve saati (cal_events/cal_hours ile aynı tanım)."""
    ev = [c for c in k["calendar"] if not c["all_day"] and c["end_utc"]]
    h = sum(min(24.0, max(0.0, (_dt(c["end_utc"]) - _dt(c["start_utc"])).total_seconds() / 3600)) for c in ev)
    return len(ev), min(24.0, h)


def numbers(hist: dict[str, dict], day: str) -> str:
    def row(label, r):
        return "| " + " | ".join([label] + [str(r.get(c, "NULL")) for c in COUNTS]) + " |"
    out = ["| gün | " + " | ".join(SHORT[c] for c in COUNTS) + " |", "|" + "---|" * (len(COUNTS) + 1)]
    for n in range(14, 0, -1):
        d = _prev(day, n)
        if d in hist:
            out.append(row(f"{d} {DOW[date.fromisoformat(d).isoweekday()]}", hist[d]))
    out.append(row("dün", yesterday(hist, day)))
    out.append(row("son 4 aynı gün ort", dow4(hist, day)))
    return "\n".join(out)


def people_text(hist: dict[str, dict], day: str, names: dict[str, str], n: int = 40) -> str:
    c = Counter(x for i in range(1, 29) for x in hist.get(_prev(day, i), {}).get("people", []))
    return "\n".join(f"- {names.get(x, x)} ({m})" for x, m in c.most_common(n)) or "- (yok)"


def name_map() -> dict[str, str]:
    with ro(cfv_db()) as c:
        return {str(i): n for i, n in c.execute("SELECT id, display_name FROM person")}


def memo_vec(day: str) -> list[float]:
    """Mini-raporun embedding'i (önbellek: persona/emb/<gün>.json; mini-rapor değişirse silin)."""
    p = out_dir("persona/emb") / f"{day}.json"
    if not p.exists():
        p.write_text(json.dumps(textscore.embed([textscore.doc(memo.memo(day))])[0]))
    return json.loads(p.read_text())


def prompt(hist: dict[str, dict], day: str, k: int, names: dict[str, str]) -> str:
    """'Günü hayal et' istemi (K olası rapor). Yalnız t'den öncesini görür."""
    assert all(d < day for d in hist), "walk-forward ihlali"
    with ro(cfv_db()) as c:
        kn = known(c, day)
    pw = _prev(day, 7)
    return DAY.format(day=day, dow=DOW[kn["dow"]], labels=memo.labels(), calendar=calendar(kn), k=k,
                      numbers=numbers(hist, day), people=people_text(hist, day, names),
                      prev_week=pw, prev_week_memo=memo.memo(pw),
                      memos="\n\n".join(memo.memo(_prev(day, n)) for n in range(7, 0, -1)))


def recall(hist: dict[str, dict], day: str, reports: list[str]) -> list[list[str]]:
    pool = [d for n in range(1, HORIZON + 1) if (d := _prev(day, n)) in hist and memo.path(d).exists()]
    vecs = {d: memo_vec(d) for d in pool}
    out = []
    for q in textscore.embed([textscore.doc(r) for r in reports]):
        out.append([d for _, d in sorted(((textscore.cos(q, v), d) for d, v in vecs.items()), reverse=True)[:ANALOGS]])
    return out


def samples(by_day: dict[str, dict], day: str, mem: dict, kcal) -> list[dict]:
    """Havuzdaki her gün bir olası gün: o günün gerçek hedefleri. Takvim ayrı: known(t) + son günlerin
    'aynı gün eklenen' olay/saat farkı (bu alışkanlık döneme bağlı; analoglardan alınca eski yoğun
    dönemler şişiriyordu)."""
    recent = [_prev(day, n) for n in range(1, RECENT + 1)]
    pool = [d for a in mem["analogs"] for d in a] + recent
    k_t = kcal(day)
    added = [(max(0, r["cal_events"] - kcal(d)[0]), max(0.0, r["cal_hours"] - kcal(d)[1]))
             for d in recent if (r := by_day.get(d, {})).get("cal_events") is not None]
    out = []
    for i, d in enumerate(pool):
        r = by_day.get(d, {})
        s = {c: r[c] for c in COUNTS + ["people", "chats"] if r.get(c) is not None and c not in ("cal_events", "cal_hours")}
        if added and r.get("cal_events") is not None:
            e, h = added[i % len(added)]
            s["cal_events"], s["cal_hours"] = k_t[0] + e, round(min(24.0, k_t[1] + h), 2)
        out.append(s)
    return out


def aggregate(day: str, ss: list[dict], reports: list[str] = ()) -> dict:
    row = {"day": day}
    for c in COUNTS:
        if v := [s[c] for s in ss if c in s]:
            row[c] = statistics.median(v)
    for c in ("people", "chats"):
        if sv := [s[c] for s in ss if c in s]:
            votes = Counter(x for v in sv for x in v)
            row[c] = sorted(x for x, m in votes.items() if m >= VOTE * len(sv))
    if reports:  # projeler: hayal edilen raporların frontmatter'ından çoğunluk
        votes = Counter(p for r in reports for p in textscore.parse(r).get("projects", ()))
        row["projects"] = sorted(p for p, m in votes.items() if m * 2 >= len(reports))
    return row


def imagined(day: str, name: str = "persona") -> Path:
    """Haiku'nun yazdığı K rapor ('===' satırıyla ayrık)."""
    return out_dir(f"{name}/imagined") / f"{day}.md"


def run(by_day: dict[str, dict], day: str, kcal, name: str = "persona") -> tuple[dict, list[dict]] | None:
    path = out_dir(f"{name}/samples") / f"{day}.json"
    if path.exists():
        mem = json.loads(path.read_text())
    elif (im := imagined(day, name)).exists():
        reports = [r.strip() for r in re.split(r"^===\s*$", im.read_text(), flags=re.M) if r.strip()]
        mem = {"reports": reports, "analogs": recall({d: r for d, r in by_day.items() if d < day}, day, reports)}
        path.write_text(json.dumps(mem, ensure_ascii=False, indent=1))
    else:
        return None
    ss = samples(by_day, day, mem, kcal)
    return aggregate(day, ss, mem["reports"]), ss
