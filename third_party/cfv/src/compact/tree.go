package main

// Hiyerarşik recursive sıkıştırma: kök = tüm veri. Bir düğümün ham verisi sınıra sığıyorsa yaprak
// (rapor ham veriden), sığmıyorsa alt katmana iner (rapor çocukların raporlarından).
// kök → yıl → ay → hafta (aya kırpılmış ISO hafta) → gün → 3 saat → 1 saat

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

var (
	Limit   = 60000 // karakter (~20k token)
	levels  = []string{"kök", "yıl", "ay", "hafta", "gün", "3 saat", "1 saat"}
	dataDir = envOr("CFV_DATA", "data")
)

const day = 24 * time.Hour

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

type node struct {
	path       []string
	start, end time.Time // end hariç
	h0, h1     int       // h1 > 0: gün içi saat aralığı
}

func (n node) level() string { return levels[len(n.path)] }
func (n node) name() string {
	if len(n.path) == 0 {
		return "kök"
	}
	return strings.Join(n.path, "/")
}
func (n node) file() string {
	return filepath.Join(append([]string{dataDir, "processed", "reports"}, append(n.path, "index.md")...)...)
}
func (n node) label() string {
	s := n.start.Format("2006-01-02")
	if last := n.end.Add(-day); !last.Equal(n.start) {
		s += " → " + last.Format("2006-01-02")
	}
	if n.h1 > 0 {
		s += fmt.Sprintf(" saat %02d-%02d", n.h0, n.h1)
	}
	return s
}

// item: tek bir kaydın özeti
type item struct {
	hour     int // -1: saatsiz (tüm gün)
	est, len int // dökümde tahmini boyu, imza için gerçek boyu
}

type tree struct {
	db   *sql.DB
	days []string          // veri olan günler, sıralı
	by   map[string][]item // gün → kayıtlar
	fit  map[string]bool
}

// dökümde kaplayacağı yaklaşık karakter
const est = `CASE kind
	WHEN 'whatsapp' THEN min(length(coalesce(body,'')), 220) + 30
	WHEN 'email' THEN 200
	WHEN 'calendar' THEN length(coalesce(title,'')) + 70
	WHEN 'note' THEN min(length(coalesce(body,'')), 600) + length(coalesce(title,'')) + 10
	WHEN 'todo' THEN length(coalesce(title,'')) + 25
	WHEN 'photo' THEN 4 WHEN 'video' THEN 4 ELSE 60 END`

func load(db *sql.DB) *tree {
	t := &tree{db: db, by: map[string][]item{}, fit: map[string]bool{}}
	rows, err := db.Query(`SELECT local_date, start_utc, coalesce(tz_offset_min,0), time_quality, `+est+`,
		length(coalesce(title,'')) + length(coalesce(body,'')) + length(coalesce(payload,''))
		FROM event WHERE kind NOT IN ('day_report','report','dream') AND local_date < ?`, time.Now().Format("2006-01-02"))
	check(err)
	defer rows.Close()
	for rows.Next() {
		var d, s, tq string
		var off int
		var it item
		check(rows.Scan(&d, &s, &off, &tq, &it.est, &it.len))
		it.hour = -1
		if tq != "day" && tq != "week" {
			it.hour = localTime(s, off).Hour()
		}
		if t.by[d] == nil {
			t.days = append(t.days, d)
		}
		t.by[d] = append(t.by[d], it)
	}
	sort.Strings(t.days)
	return t
}

func (t *tree) daysIn(n node) []string {
	a := sort.SearchStrings(t.days, n.start.Format("2006-01-02"))
	b := sort.SearchStrings(t.days, n.end.Format("2006-01-02"))
	return t.days[a:b]
}

func (t *tree) items(n node) []item {
	var out []item
	for _, d := range t.daysIn(n) {
		for _, it := range t.by[d] {
			if n.h1 == 0 || it.hour >= n.h0 && it.hour < n.h1 {
				out = append(out, it)
			}
		}
	}
	return out
}

func (t *tree) root() node {
	a, _ := time.Parse("2006-01-02", t.days[0])
	b, _ := time.Parse("2006-01-02", t.days[len(t.days)-1])
	return node{start: a, end: b.Add(day)}
}

// raw: düğümün ham verisi (yaprak girdisi)
func (t *tree) raw(n node) string {
	if len(n.path) >= 4 {
		return dossier(t.db, n.start.Format("2006-01-02"), n.h0, n.h1, false, true)
	}
	var parts []string
	for _, d := range t.daysIn(n) {
		parts = append(parts, dossier(t.db, d, 0, 0, false, false))
	}
	return strings.Join(parts, "\n")
}

// fits: tahmin sınırdan çok uzaksa ona güven, yakınsa gerçek metni ölç. 1 saat her zaman yaprak.
func (t *tree) fits(n node) bool {
	k := n.name()
	if v, ok := t.fit[k]; ok {
		return v
	}
	size := 300 * len(t.daysIn(n))
	for _, it := range t.items(n) {
		size += it.est
	}
	ok := len(n.path) == 6 || size < Limit/2 || size <= Limit*2 && len(t.raw(n)) < Limit
	t.fit[k] = ok
	return ok
}

func (t *tree) children(n node) []node {
	var out []node
	add := func(name string, a, b time.Time, h0, h1 int) {
		c := node{append(append([]string{}, n.path...), name), a, b, h0, h1}
		if len(t.items(c)) > 0 {
			out = append(out, c)
		}
	}
	switch len(n.path) {
	case 0:
		for y := n.start.Year(); y <= n.end.Year(); y++ {
			a := time.Date(y, 1, 1, 0, 0, 0, 0, time.UTC)
			add(fmt.Sprint(y), a, a.AddDate(1, 0, 0), 0, 0)
		}
	case 1:
		for m := 1; m <= 12; m++ {
			a := time.Date(n.start.Year(), time.Month(m), 1, 0, 0, 0, 0, time.UTC)
			add(fmt.Sprintf("%02d", m), a, a.AddDate(0, 1, 0), 0, 0)
		}
	case 2: // ISO haftalar, aya kırpılmış
		for a := n.start; a.Before(n.end); {
			_, w := a.ISOWeek()
			b := a.Add(day)
			for ; b.Before(n.end) && b.Weekday() != time.Monday; b = b.Add(day) {
			}
			add(fmt.Sprintf("W%02d", w), a, b, 0, 0)
			a = b
		}
	case 3:
		for a := n.start; a.Before(n.end); a = a.Add(day) {
			add(fmt.Sprintf("%02d", a.Day()), a, a.Add(day), 0, 0)
		}
	case 4, 5:
		step, h0, h1 := 3, 0, 24
		if len(n.path) == 5 {
			step, h0, h1 = 1, n.h0, n.h1
		}
		for h := h0; h < h1; h += step {
			add(fmt.Sprintf("%02d-%02d", h, h+step), n.start, n.end, h, h+step)
		}
	}
	return out
}

func (t *tree) split(n node) []node {
	if t.fits(n) {
		return nil
	}
	return t.children(n)
}

// sig: yaprak/bölünmüş + kayıt sayısı + toplam uzunluk. Aralığa veri eklenince değişir.
func (t *tree) sig(n node) string {
	kind, total, its := "yaprak", 0, t.items(n)
	if len(t.split(n)) > 0 {
		kind = "bölünmüş"
	}
	for _, it := range its {
		total += it.len
	}
	return fmt.Sprintf("%s:%d:%d", kind, len(its), total)
}

// find: "2026/10/W43/22" → düğüm
func (t *tree) find(name string) (node, error) {
	n := t.root()
	for _, p := range strings.Split(strings.Trim(name, "/"), "/") {
		if p == "" || p == "kök" {
			continue
		}
		found := false
		for _, c := range t.children(n) {
			if c.path[len(c.path)-1] == p {
				n, found = c, true
				break
			}
		}
		if !found {
			return n, fmt.Errorf("%s: %q yok (o aralıkta veri yok)", name, p)
		}
	}
	return n, nil
}

// walk: çocuklar önce (post-order)
func (t *tree) walk(n node, f func(node)) {
	for _, c := range t.split(n) {
		t.walk(c, f)
	}
	f(n)
}

var reField = func(k string) *regexp.Regexp { return regexp.MustCompile(`(?m)^` + k + `:\s*(.+?)\s*$`) }
var reSig, reRange = reField("sig"), reField("range")

func field(file string, re *regexp.Regexp) string {
	b, err := os.ReadFile(file)
	if m := re.FindSubmatch(b); err == nil && m != nil {
		return string(m[1])
	}
	return ""
}

// stale: raporu olmayan ya da imzası değişmiş düğümler, çocuklar önce
func (t *tree) stale(from node) []node {
	var out []node
	t.walk(from, func(n node) {
		if field(n.file(), reSig) != t.sig(n) {
			out = append(out, n)
		}
	})
	return out
}

// previous: aynı derinlikte, bu düğümden önce biten en son rapor (süreklilik için)
func previous(n node) string {
	if len(n.path) == 0 {
		return ""
	}
	pattern := filepath.Join(append([]string{dataDir, "processed", "reports"}, strings.Split(strings.Repeat("*/", len(n.path))+"index.md", "/")...)...)
	files, _ := filepath.Glob(pattern)
	best, bestEnd := "", ""
	for _, f := range files {
		r := strings.Fields(field(f, reRange))
		if len(r) == 0 {
			continue
		}
		end := r[0]
		if len(r) > 2 && r[1] == "→" {
			end = r[2]
		}
		if end < n.start.Format("2006-01-02") && end > bestEnd {
			best, bestEnd = f, end
		}
	}
	return best
}

func (t *tree) input(n node) string {
	var b strings.Builder
	w := func(format string, a ...any) { fmt.Fprintf(&b, format+"\n", a...) }
	w("# İŞ: %s raporu — %s (%s)", n.level(), n.name(), n.label())
	w("Yaz: %s", n.file())
	w("Frontmatter'a aynen koy → level: %s | node: %s | range: %s | sig: %s", n.level(), n.name(), n.label(), t.sig(n))
	w("Biçim: src/compact/report.md · Kişisel bağlam: data/config/day-context.md\n")
	if p := previous(n); p != "" {
		prev, _ := os.ReadFile(p)
		w("=== ÖNCEKİ %s RAPORU — süreklilik için ===\n%s\n", strings.ToUpper(n.level()), strings.TrimSpace(string(prev)))
	}
	kids, body := t.split(n), ""
	if len(kids) == 0 {
		w("=== HAM VERİ ===")
		body = t.raw(n)
	} else {
		w("=== ALT RAPORLAR (%d %s; ayrıntı için: compact input <düğüm>) ===", len(kids), kids[0].level())
		var parts []string
		if len(n.path) == 4 { // bölünmüş gün: saatsiz kayıtlar (takvim, ödev, sağlık) iskelette
			parts = append(parts, dossier(t.db, n.start.Format("2006-01-02"), 0, 0, true, true))
		}
		for _, c := range kids {
			r, err := os.ReadFile(c.file())
			if err != nil {
				r = []byte("(RAPOR YOK — önce bu yazılmalı)")
			}
			parts = append(parts, fmt.Sprintf("--- %s: %s (%s) ---\n%s", c.level(), c.name(), c.label(), strings.TrimSpace(string(r))))
		}
		body = strings.Join(parts, "\n\n")
	}
	if len(body) > Limit {
		body = body[:Limit] + fmt.Sprintf("\n… (sınır: %d karakter kesildi)", len(body)-Limit)
	}
	w("%s", body)
	return b.String()
}
