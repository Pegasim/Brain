// photos: her fotoğraf/videoyu bir LLM'e (Haiku) değerlendirtmek için paket hazırlar.
//
//	photos next <işçi> <işçi sayısı> [n]   değerlendirilmemiş n (25) medya → paket dosyası, yolunu yazar
//	photos signals                        favori/kopya/WhatsApp sinyalleri → signals.json
//	photos score                          LLM puanı + sinyal bonusu → scores.tsv (yüksekten düşüğe)
//	photos status                         kaç medya var, kaçı değerlendirildi
//
// Paket: her medya için küçük JPEG (video: 6 kare) + bilgi kartı. Değerlendirme
// data/processed/photos/eval/<id>.json'a yazılır; dosya varsa medya bitmiş sayılır.
package main

import (
	"bufio"
	"database/sql"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

var (
	dataDir = envOr("CFV_DATA", "data")
	out     = filepath.Join(dataDir, "processed", "photos")
	days    = []string{"Pazar", "Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi"}
)

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func exists(p string) bool { _, err := os.Stat(p); return err == nil }

// dosyalar kalıcı kimlikle (iCloud asset UUID) adlanır: yeniden ingest event id'lerini değiştirir
func evalFile(key string) string { return filepath.Join(out, "eval", fname(key)+".json") }

// fname: UUID'siz medyanın anahtarı 'file:<yol>' → dosya adında : ve / olmaz
func fname(key string) string { return strings.NewReplacer(":", "-", "/", "_").Replace(key) }

// medya: dosyası diskte olan foto/videolar; düzenlenmiş sürüm varsa o (Tuna'nın beğendiği hâl)
const media = `SELECT e.id, e.external_id, e.kind, e.start_utc, coalesce(e.tz_offset_min,180), e.local_date,
	coalesce(e.title,''), coalesce(e.body,''), coalesce(json_extract(e.payload,'$.duration_s'),0),
	(SELECT m.path FROM media m WHERE m.event_id = e.id AND m.role IN ('adjusted','original','alt')
	 ORDER BY m.role = 'adjusted' DESC, m.role = 'original' DESC LIMIT 1) AS path
	FROM event e WHERE e.kind IN ('photo','video')`

// notes: data/config/people.md → isim: Tuna'nın notu
func notes() map[string]string {
	m := map[string]string{}
	f, err := os.Open(filepath.Join(dataDir, "config", "people.md"))
	if err != nil {
		return m
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		l := strings.TrimPrefix(s.Text(), "- ")
		i, j := strings.Index(l, " ("), strings.Index(l, "): ")
		if i > 0 && j > i && strings.TrimSpace(l[j+3:]) != "" {
			m[l[:i]] = strings.TrimSpace(l[j+3:])
		}
	}
	return m
}

func strs(db *sql.DB, q string, args ...any) []string {
	rows, err := db.Query(q, args...)
	check(err)
	defer rows.Close()
	var r []string
	for rows.Next() {
		var s sql.NullString
		check(rows.Scan(&s))
		r = append(r, s.String)
	}
	return r
}

// thumb: 768 px JPEG; video için 6 karelik bir sayfa
func thumb(key, kind, src string, dur float64) (string, error) {
	dst, _ := filepath.Abs(filepath.Join(out, "thumbs", fname(key)+".jpg"))
	if exists(dst) {
		return dst, nil
	}
	cmd := exec.Command("vipsthumbnail", src, "--size", "768", "-o", dst+"[Q=80,strip]")
	if kind == "video" {
		if dur <= 0 {
			dur = 6
		}
		cmd = exec.Command("ffmpeg", "-v", "error", "-i", src, "-vf",
			fmt.Sprintf("fps=6/%.1f,scale=384:-2,tile=3x2", dur), "-frames:v", "1", "-y", dst)
	}
	b, err := cmd.CombinedOutput()
	if err != nil && kind != "video" { // vips'in açamadığı HEIC (derinlik haritalı Portre): derive.py mid/ aynı biçimde (pillow-heif)
		if m, e := os.ReadFile(filepath.Join(out, "mid", fname(key)+".jpg")); e == nil && os.WriteFile(dst, m, 0o644) == nil {
			return dst, nil
		}
	}
	if err != nil { // açılamayan dosya: .err ile işaretle, tekrar denenmez
		os.WriteFile(strings.TrimSuffix(dst, ".jpg")+".err", b, 0o600)
		return "", fmt.Errorf("%s: %v", src, err)
	}
	return dst, nil
}

func next(db *sql.DB, worker, of, n int) {
	for _, d := range []string{"thumbs", "eval", "batches"} {
		check(os.MkdirAll(filepath.Join(out, d), 0o700))
	}
	batch := func(text string) {
		p := filepath.Join(out, "batches", fmt.Sprintf("w%d-%s.md", worker, time.Now().Format("0102-150405")))
		check(os.WriteFile(p, []byte(text), 0o600))
		fmt.Println(p)
	}
	if text := stickerBatch(worker, of, n); text != "" { // önce stickerlar (az ve kaynak fotoyu gösteriyorlar)
		batch(text)
		return
	}
	rows, err := db.Query(media+` AND e.id % ? = ? ORDER BY (e.id * 7919) % 10007`, of, worker%of)
	check(err)
	type item struct {
		id                                     int64
		key, kind, utc, day, title, body, path string
		off                                    int
		dur                                    float64
	}
	var items []item
	for rows.Next() && len(items) < n {
		var it item
		var path sql.NullString
		check(rows.Scan(&it.id, &it.key, &it.kind, &it.utc, &it.off, &it.day, &it.title, &it.body, &it.dur, &path))
		if it.path = path.String; path.Valid && exists(it.path) && !exists(evalFile(it.key)) &&
			!exists(filepath.Join(out, "thumbs", fname(it.key)+".err")) {
			items = append(items, it)
		}
	}
	rows.Close()
	if len(items) == 0 {
		fmt.Println("BİTTİ")
		return
	}
	who, sigs, apps := notes(), loadSignals(), loadApps()
	var b strings.Builder
	fmt.Fprintf(&b, "# Paket (%d medya) — talimat: src/photos/rate.md\n\n", len(items))
	for _, it := range items {
		img, err := thumb(it.key, it.kind, it.path, it.dur)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			continue
		}
		t, _ := time.Parse(time.RFC3339, it.utc)
		t = t.Add(time.Duration(it.off) * time.Minute)
		fmt.Fprintf(&b, "## %s\nGörsel: %s", it.key, img)
		if it.kind == "video" {
			fmt.Fprintf(&b, " (VİDEO, %.0f sn; 6 kare soldan sağa, yukarıdan aşağı)", it.dur)
		}
		fmt.Fprintf(&b, "\nZaman: %s (%s)\nDosya: %s\n", t.Format("2006-01-02 15:04"), days[t.Weekday()], it.title)
		for _, l := range strings.Split(it.body, "\n") { // Yer, Albüm, açıklama (kişiler aşağıda notlarıyla)
			if l = strings.TrimSpace(l); l != "" && !strings.HasPrefix(l, "Kişiler:") {
				fmt.Fprintf(&b, "%s\n", l)
			}
		}
		var ppl []string
		for _, p := range strs(db, `SELECT p.display_name FROM event_person ep JOIN person p ON p.id = ep.person_id WHERE ep.event_id = ?`, it.id) {
			if note := who[p]; note != "" {
				p += " — " + note
			}
			ppl = append(ppl, p)
		}
		if len(ppl) > 0 {
			fmt.Fprintf(&b, "Kişiler (iCloud yüz tanıma): %s\n", strings.Join(ppl, "; "))
		}
		switch a := apps[it.key]; a {
		case "":
		case "gorsel":
			fmt.Fprintf(&b, "Ekran: iPhone ekran görüntüsü diye işaretlemiş; otomatik tahmin ekranın/kâğıdın fotoğrafı olabileceği (ipucu, kesin değil)\n")
		default:
			fmt.Fprintf(&b, "Ekran: iPhone ekran görüntüsü diye işaretlemiş; otomatik uygulama tahmini %s (ipucu, kesin değil)\n", a)
		}
		if l := sigs[it.key].line(); l != "" {
			fmt.Fprintf(&b, "Sinyaller: %s\n", l)
		}
		cnt := strs(db, `SELECT count(*) FROM event WHERE kind IN ('photo','video') AND local_date = ?`, it.day)
		cal := strs(db, `SELECT title FROM event WHERE kind = 'calendar' AND local_date = ? LIMIT 8`, it.day)
		fmt.Fprintf(&b, "O gün: %s foto/video", cnt[0])
		if len(cal) > 0 {
			fmt.Fprintf(&b, "; takvim: %s", strings.Join(cal, ", "))
		}
		fmt.Fprintf(&b, "\nYaz: %s\n\n", evalFile(it.key))
	}
	batch(b.String())
}

// loadApps: apps.py tahmin çıktısı (data/processed/photos/apps.tsv): ekran görüntüsü → olası uygulama
func loadApps() map[string]string {
	m := map[string]string{}
	if b, err := os.ReadFile(filepath.Join(out, "apps.tsv")); err == nil {
		for _, l := range strings.Split(string(b), "\n")[1:] {
			if c := strings.Split(l, "\t"); len(c) > 1 {
				m[c[0]] = c[1]
			}
		}
	}
	return m
}

func main() {
	db, err := sql.Open("sqlite", "file:"+filepath.Join(dataDir, "processed", "cfv.sqlite")+"?mode=ro")
	check(err)
	arg := func(i, def int) int {
		if len(os.Args) > i {
			v, err := strconv.Atoi(os.Args[i])
			check(err)
			return v
		}
		return def
	}
	switch c := append(os.Args, "")[1]; c {
	case "next":
		next(db, arg(2, 0), arg(3, 1), arg(4, 25))
	case "signals":
		signals(db)
	case "score":
		score(db)
	case "status":
		var all, onDisk, done int
		rows, err := db.Query(`SELECT external_id, path FROM (` + media + `)`)
		check(err)
		for rows.Next() {
			var id string
			var p sql.NullString
			check(rows.Scan(&id, &p))
			all++
			if p.Valid && exists(p.String) {
				onDisk++
			}
			if exists(evalFile(id)) {
				done++
			}
		}
		fmt.Printf("medya %d, diskte %d, değerlendirilen %d\n", all, onDisk, done)
	default:
		check(fmt.Errorf("kullanım: photos next <işçi> <sayı> [n] | signals | score | status"))
	}
}
