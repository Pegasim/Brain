package main

// Bir günün (ya da gün içi saat aralığının) verisini LLM'e verilecek düz metne döker.

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
)

type event struct {
	id                                int64
	kind, start, end, tq, title, body string
	off                               int
	p                                 map[string]any
}

// yerel zaman; saat farkı bilinmiyorsa İstanbul (+180 dk)
func localTime(utc string, off int) time.Time {
	t, _ := time.Parse(time.RFC3339, utc)
	if off == 0 {
		off = 180
	}
	return t.Add(time.Duration(off) * time.Minute)
}

func (e event) at() time.Time { return localTime(e.start, e.off) }
func (e event) hm() string    { return e.at().Format("15:04") }
func (e event) timed() bool   { return e.tq != "day" && e.tq != "week" }
func (e event) str(k string) string {
	if v, ok := e.p[k]; ok && v != nil {
		return fmt.Sprint(v)
	}
	return ""
}

func cut(s string, n int) string {
	s = strings.Join(strings.Fields(s), " ")
	if r := []rune(s); len(r) > n {
		return string(r[:n-1]) + "…"
	}
	return s
}

func loadEvents(db *sql.DB, where string, args ...any) []event {
	rows, err := db.Query(`SELECT id, kind, start_utc, coalesce(end_utc,''), coalesce(tz_offset_min,0),
		time_quality, coalesce(title,''), coalesce(body,''), coalesce(payload,'{}')
		FROM event WHERE kind NOT IN ('day_report','report','dream') AND `+where+` ORDER BY start_utc`, args...)
	check(err)
	defer rows.Close()
	var out []event
	for rows.Next() {
		var e event
		var p string
		check(rows.Scan(&e.id, &e.kind, &e.start, &e.end, &e.off, &e.tq, &e.title, &e.body, &p))
		json.Unmarshal([]byte(p), &e.p)
		out = append(out, e)
	}
	return out
}

func countMap(db *sql.DB, q, day string) map[int64][]string {
	out := map[int64][]string{}
	rows, err := db.Query(q, day)
	check(err)
	defer rows.Close()
	for rows.Next() {
		var id int64
		var s string
		check(rows.Scan(&id, &s))
		out[id] = append(out[id], s)
	}
	return out
}

// top: "a×3, b×2" biçiminde en sık n
func top(c map[string]int, n int) string {
	keys := make([]string, 0, len(c))
	for k := range c {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool { return c[keys[i]] > c[keys[j]] || c[keys[i]] == c[keys[j]] && keys[i] < keys[j] })
	var parts []string
	for i, k := range keys {
		if i == n {
			break
		}
		parts = append(parts, fmt.Sprintf("%s×%d", k, c[k]))
	}
	return strings.Join(parts, ", ")
}

// byKey: "00:00×4, 09:00×2" biçiminde anahtara göre sıralı
func byKey(c map[string]int) string {
	var parts []string
	for k, n := range c {
		parts = append(parts, fmt.Sprintf("%s×%d", k, n))
	}
	sort.Strings(parts)
	return strings.Join(parts, ", ")
}

var weekdays = []string{"Pazar", "Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi"}

// dossier: h1 > 0 ise yalnız [h0,h1) saatlerindeki saatli kayıtlar. skeleton: foto/mail/mesaj
// yerine sayılar (bölünmüş günün iskeleti). weekTodos: önceki 6 günün "hafta" ödevlerini de ekle.
func dossier(db *sql.DB, day string, h0, h1 int, skeleton, weekTodos bool) string {
	var evs []event
	for _, e := range loadEvents(db, "local_date = ?", day) {
		if h := e.at().Hour(); h1 == 0 || e.timed() && h >= h0 && h < h1 {
			evs = append(evs, e)
		}
	}
	by := map[string][]event{}
	counts := map[string]int{}
	for _, e := range evs {
		by[e.kind] = append(by[e.kind], e)
		counts[e.kind]++
	}
	people := countMap(db, `SELECT ep.event_id, p.display_name FROM event_person ep JOIN person p ON p.id = ep.person_id
		JOIN event e ON e.id = ep.event_id WHERE e.local_date = ? AND e.kind != 'dream'`, day)
	calPhotos := countMap(db, `SELECT l.b, l.a FROM event_link l JOIN event c ON c.id = l.b
		WHERE l.kind = 'photo_at' AND c.local_date = ?`, day)

	var b strings.Builder
	w := func(format string, a ...any) { fmt.Fprintf(&b, format+"\n", a...) }
	d, _ := time.Parse("2006-01-02", day)
	suffix := ""
	if h1 > 0 {
		suffix = fmt.Sprintf(" saat %02d-%02d", h0, h1)
	} else if skeleton {
		suffix = " — iskelet"
	}
	w("# %s (%s)%s\n", day, weekdays[d.Weekday()], suffix)
	var cs []string
	for k, n := range counts {
		cs = append(cs, fmt.Sprintf("%s=%d", k, n))
	}
	sort.Strings(cs)
	if len(cs) == 0 {
		cs = []string{"veri yok"}
	}
	w("Sayılar: %s\n", strings.Join(cs, ", "))

	if es := by["calendar"]; len(es) > 0 {
		w("## Takvim")
		for _, e := range es {
			when := "tüm gün"
			if e.timed() {
				when = e.hm() + "-?"
				if e.end != "" {
					when = e.hm() + "-" + localTime(e.end, e.off).Format("15:04")
				}
			}
			extra := ""
			if loc := e.str("location"); loc != "" {
				extra += " | yer: " + loc
			}
			if n := len(calPhotos[e.id]); n > 0 {
				extra += fmt.Sprintf(" | o sırada %d foto", n)
			}
			w("- %s %s [%s]%s", when, e.title, e.str("calendar"), extra)
		}
		w("")
	}

	if h1 == 0 {
		todos := by["todo"]
		if weekTodos {
			todos = append(todos, loadEvents(db, `kind = 'todo' AND time_quality = 'week'
				AND local_date BETWEEN date(?, '-6 days') AND ? AND local_date != ?`, day, day, day)...)
		}
		if len(todos) > 0 {
			w("## Ödev/yapılacaklar ([x] yapıldı, [ ] açık; 'hafta' = günü belirsiz)")
			for _, e := range todos {
				box := map[string]string{"true": "[x]", "false": "[ ]"}[e.str("done")]
				if box == "" {
					box = "-"
				}
				wk, sec := "", ""
				if e.tq == "week" {
					wk = "hafta: "
				}
				if s := e.str("section"); s != "" {
					sec = " (" + s + ")"
				}
				w("- %s %s%s%s", box, wk, e.title, sec)
			}
			w("")
		}
	}

	if es := by["note"]; len(es) > 0 {
		w("## Notlar (bu gün oluşturulan)")
		for _, e := range es {
			w("- %s: %s", e.title, cut(e.body, 600))
		}
		w("")
	}

	detail := []string{"photo", "video", "email", "whatsapp"}
	if skeleton {
		n, blocks := map[string]int{}, map[string]int{}
		for _, k := range detail {
			for _, e := range by[k] {
				n[k]++
				blocks[fmt.Sprintf("%02d:00", e.at().Hour()/3*3)]++
			}
			delete(by, k)
		}
		if len(n) > 0 {
			w("## Ayrıntısı alt raporlarda olanlar\n- %s\n- 3 saatlik bloklara göre: %s\n", top(n, 9), byKey(blocks))
		}
	}

	if media := append(by["photo"], by["video"]...); len(media) > 0 {
		sort.Slice(media, func(i, j int) bool { return media[i].start < media[j].start })
		w("## Fotoğraf/video (%d foto, %d video, %s–%s)", len(by["photo"]), len(by["video"]), media[0].hm(), media[len(media)-1].hm())
		hours, ppl, places := map[string]int{}, map[string]int{}, map[string]int{}
		var desc []string
		for _, e := range media {
			hours[e.at().Format("15")+":00"]++
			for _, p := range people[e.id] {
				ppl[p]++
			}
			if m := e.str("moment"); m != "" {
				places[m]++
			}
			if strings.Contains(e.body, "Açıklama") && len(desc) < 10 {
				desc = append(desc, cut(e.body, 120))
			}
		}
		w("Saatlere göre: %s", byKey(hours))
		if len(ppl) > 0 {
			w("Fotoğraflardaki kişiler: %s", top(ppl, 15))
		}
		if len(places) > 0 {
			w("Yerler: %s", top(places, 8))
		}
		if len(desc) > 0 {
			w("Açıklamalar: %s", strings.Join(desc, " | "))
		}
		w("")
	}

	if es := by["email"]; len(es) > 0 {
		w("## E-posta (%d)", len(es))
		for i, e := range es {
			if i == 60 {
				break
			}
			arrow, who := "←", e.str("from")
			if e.str("direction") == "sent" {
				arrow, who = "→", e.str("to")
			}
			w("- %s %s %s: %s", e.hm(), arrow, cut(who, 60), cut(e.title, 120))
		}
		w("")
	}

	if es := by["whatsapp"]; len(es) > 0 {
		chats := map[string][]event{}
		var order []string
		for _, e := range es {
			c := e.str("chat")
			if c == "" {
				c = e.title
			}
			if chats[c] == nil {
				order = append(order, c)
			}
			chats[c] = append(chats[c], e)
		}
		sort.SliceStable(order, func(i, j int) bool { return len(chats[order[i]]) > len(chats[order[j]]) })
		w("## WhatsApp (%d mesaj, %d sohbet)", len(es), len(chats))
		for _, c := range order {
			msgs, mine := chats[c], 0
			for _, e := range msgs {
				if e.str("from_me") == "true" {
					mine++
				}
			}
			typ := map[string]string{"group": "grup", "dm": "birebir"}[msgs[0].str("chat_type")]
			w("### %s (%s; %d mesaj, %d benden)", c, typ, len(msgs), mine)
			for _, e := range msgs {
				who := e.str("sender")
				if e.str("from_me") == "true" {
					who = "ben"
				} else if who == "" {
					who = c
				}
				w("  %s %s: %s", e.hm(), who, cut(e.body, 220))
			}
		}
		w("")
	}

	if h1 == 0 {
		var health []string
		for _, e := range by["zepp_day"] {
			health = append(health, fmt.Sprintf("adım %s, dinlenme nabzı %s, PAI %s", e.str("steps"), e.str("rest_hr"), e.str("pai")))
		}
		for _, e := range by["sleep"] {
			min, _ := e.p["asleep_min"].(float64)
			health = append(health, fmt.Sprintf("uyku %.1f sa (%s→%s)", min/60, e.hm(), localTime(e.end, e.off).Format("15:04")))
		}
		for _, e := range by["workout"] {
			health = append(health, "egzersiz: "+e.title+" "+e.hm())
		}
		if len(health) > 0 {
			w("## Sağlık (sayılar)\n- %s\n", strings.Join(health, "\n- "))
		}
	}

	known := map[string]bool{"calendar": true, "todo": true, "note": true, "photo": true, "video": true,
		"email": true, "whatsapp": true, "zepp_day": true, "sleep": true, "workout": true}
	var other []string
	for k := range by {
		if !known[k] {
			other = append(other, k)
		}
	}
	sort.Strings(other)
	for _, k := range other {
		es := by[k]
		w("## %s (%d)", k, len(es))
		for i, e := range es {
			if i < 50 {
				w("- %s %s", e.hm(), cut(e.title, 120))
			}
		}
		w("")
	}
	return b.String()
}
