"""persona: GPU'suz parçalar — tamamen SENTETİK veri."""

from persona import predict as P


def _hist():
    return {"2025-03-01": {"msgs_sent": 10, "people": ["1", "2"], "chats": ["a"]},
            "2025-03-02": {"msgs_sent": 20, "people": ["1"], "chats": ["a", "b"]}}


def test_numbers_only_past_and_baselines():
    txt = P.numbers(_hist(), "2025-03-03")
    assert "2025-03-02 Pazar" in txt and "2025-03-03" not in txt
    assert "son 4 aynı gün" in txt


def test_known_cal_counts_timed_events_only():
    k = {"calendar": [{"all_day": True, "start_utc": "2025-03-03T00:00:00Z", "end_utc": None, "title": "x"},
                      {"all_day": False, "start_utc": "2025-03-03T08:00:00Z", "end_utc": "2025-03-03T09:30:00Z", "title": "y"}]}
    assert P.known_cal(k) == (1, 1.5)


def test_samples_shift_calendar_and_aggregate():
    by = {"2025-03-01": {"cal_events": 3, "cal_hours": 4.0, "msgs_sent": 10, "people": ["1"]},
          "2025-03-02": {"cal_events": 1, "cal_hours": 1.0, "msgs_sent": 30, "people": ["1", "2"]}}
    kcal = {"2025-03-03": (2, 2.0), "2025-03-01": (1, 1.0), "2025-03-02": (1, 1.0)}.get
    old = P.RECENT
    P.RECENT = 2
    try:
        ss = P.samples(by, "2025-03-03", {"analogs": []}, kcal)
    finally:
        P.RECENT = old
    assert [s["cal_events"] for s in ss] == [2, 4]  # bilinen 2 + son günlerin sonradan eklenenleri (0, 2)
    r = P.aggregate("2025-03-03", ss, ["---\nprojects: [okul]\n---\n# x"])
    assert r["msgs_sent"] == 20 and r["people"] == ["1", "2"] and r["projects"] == ["okul"]


def test_textscore_parse_and_soft_f1():
    from persona import textscore as T
    md = "---\nprojects: [okul, proje]\npeople: [Ali]\n---\n# 2025-03-03 — Sınav günü\n\nÖzet.\n\n## Neler oldu\n- Sınava girildi [takvim]\n- Yazışıldı [wp]\n\n## Somut çıktılar\n- —\n"
    p = T.parse(md)
    assert p["projects"] == {"okul", "proje"} and p["people"] == {"ali"}
    assert p["title"] == "Sınav günü" and p["bullets"] == ["Sınava girildi", "Yazışıldı"]
    assert T.score(md, md, use_embed=False)["projects"] == 1.0
    assert T.soft_f1([[0.9, 0.1], [0.2, 0.3]]) == (0.5, 0.5, 0.5)
