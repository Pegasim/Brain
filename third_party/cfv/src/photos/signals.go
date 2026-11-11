package main

// Fotoğraf çevresinden sayısal sinyaller (LLM'e kartla verilir, puana eklenir):
//   - favori: 1; art arda ≥20 favorinin içindeyse 0.5 (toplu yanlış favori)
//   - kopya: aynı görüntü ve türevleri galeride kaç farklı güne yayılmış (dHash kopyaları + derive.py'nin
//     bulduğu ekran görüntüsü/kaydedilmiş/kesilmiş türevler, derivs.tsv)
//   - WhatsApp: kime gönderdin, kimden geldi, geldiğinde güldün mü, tepkiler (photos-meta wa_reaction)
//   - Fotoğraflar uygulaması: kaç kez açtın/paylaştın, açıklama yazdın mı, düzenledin mi,
//     kendi Anı'na koydun mu, Apple öne çıkardı mı (photos-meta engagement/selection)

import (
	"bufio"
	"database/sql"
	"encoding/json"
	"fmt"
	"image/png"
	"math"
	"math/bits"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	favStreak = 20 // bu kadar art arda favori → yarım favori
	sameImg   = 24 // 256 bitlik dHash Hamming mesafesi ≤ bu → aynı görüntü
	laughMin  = 10 // görüntü geldikten sonra kaç dakika içindeki cevabına bakılır
)

var laugh = regexp.MustCompile(`(?i)😂|🤣|💀|😭|ahah|haha|jsjs|sjsj|ksks|kdkd|asdf|lol|lmao|öldüm|gülmekten`)

type sig struct {
	Fav      float64  `json:"favori,omitempty"`
	Copies   int      `json:"kopya,omitempty"`               // küme: kaynak + dHash kopyaları + türevler
	Days     int      `json:"kopya_farkli_gun,omitempty"`    // kaynağın günü dışındaki farklı gün sayısı
	SpanDays int      `json:"kopya_yayilim_gun,omitempty"`   // ilk-son arası gün
	UISrc    bool     `json:"kopya_kaynak_arayuz,omitempty"` // kaynak arayüz görüntüsü (PNG/ekran boyutu/ss): bonus yok
	SentTo   []string `json:"gonderdigin,omitempty"`
	From     []string `json:"gelen,omitempty"`
	Laughed  int      `json:"guldun,omitempty"`
	MyReacts string   `json:"tepkin,omitempty"`
	Reacts   int      `json:"tepki_aldi,omitempty"`
	Views    int      `json:"acilma,omitempty"`
	Shares   int      `json:"paylasma,omitempty"`
	Caption  bool     `json:"aciklama,omitempty"`
	Edited   bool     `json:"duzenlendi,omitempty"`
	MyMemory bool     `json:"kendi_anin,omitempty"`
	Featured bool     `json:"apple_one_cikardi,omitempty"`
	Sticker  int      `json:"sticker,omitempty"` // bu fotodan yapılmış stickerı kaç kez gönderdin (+1 = yapılmış)
}

// line: kart için tek satır
func (s sig) line() string {
	var p []string
	add := func(ok bool, f string, a ...any) {
		if ok {
			p = append(p, fmt.Sprintf(f, a...))
		}
	}
	add(s.Fav == 1, "favorilerde")
	add(s.Fav == 0.5, "favorilerde (toplu favorileme içinde, yarım sayılır)")
	add(s.Copies > 1 && s.Days == 0, "galeride %d kopyası/türevi var, hepsi aynı gün", s.Copies)
	add(s.Days > 0, "%d farklı günde yeniden kaydedilmiş/ekran görüntüsü alınmış (%d kopya/türev), %d gün arayla%s",
		s.Days, s.Copies-1, s.SpanDays, map[bool]string{true: " (kaynak bir arayüz görüntüsü)"}[s.UISrc])
	add(s.Views > 0, "%d kez açıp baktın", s.Views)
	add(s.Shares > 0, "%d kez paylaştın", s.Shares)
	add(s.Caption, "altına açıklama yazdın")
	add(s.Edited, "düzenledin")
	add(s.MyMemory, "kendi oluşturduğun bir Anı'da")
	add(s.Featured, "Apple öne çıkanlara seçti")
	add(s.Sticker > 0, "bundan sticker yapılmış, %d kez gönderdin", s.Sticker-1)
	add(len(s.SentTo) > 0, "WhatsApp'ta gönderdin: %s", strings.Join(s.SentTo, ", "))
	add(s.Reacts > 0, "gönderdiğinde %d tepki aldı", s.Reacts)
	add(len(s.From) > 0, "WhatsApp'ta geldi: %s", strings.Join(s.From, ", "))
	add(s.MyReacts != "", "gelince tepki verdin: %s", s.MyReacts)
	add(s.Laughed > 0, "gelince %d kez güldün", s.Laughed)
	return strings.Join(p, "; ")
}

// bonus: puana eklenen kısım (README: "Fotoğraf puanı")
func (s sig) bonus() float64 {
	b := 1.5 * s.Fav
	if s.Days > 0 && s.SpanDays >= 30 && !s.UISrc { // yakın tarihli birkaç kopya önemsiz, uzun aralıkta çok gün önemli
		b += min(2, float64(s.Days)*0.5)
	}
	b += min(1.5, math.Log2(1+float64(s.Views))/4) + min(1, 0.5*float64(s.Shares))
	b += map[bool]float64{true: 1}[s.Caption] + map[bool]float64{true: 0.5}[s.Edited]
	b += map[bool]float64{true: 1}[s.MyMemory] + map[bool]float64{true: 0.5}[s.Featured]
	b += min(1.5, 0.5*float64(len(s.SentTo))) + min(1, 0.25*float64(s.Reacts))
	if s.Sticker > 0 { // sticker yapılan fotolar genelde ikoniktir
		b += 1.5 + min(1, float64(s.Sticker-1)/10)
	}
	return b + min(1, 0.5*float64(s.Laughed+len([]rune(s.MyReacts))))
}

// hash: 256 bitlik dHash (64 bit belge/beyaz tahta gibi az ayrıntılı görüntüleri karıştırıyordu)
type hash [4]uint64

func dist(a, b hash) (n int) {
	for i := range a {
		n += bits.OnesCount64(a[i] ^ b[i])
	}
	return n
}

// dhash: 17x16 gri → 256 bit (her satırda soldaki > sağdaki)
func dhash(src, tmp string) (h hash, err error) {
	if b, err := exec.Command("vips", "thumbnail", src, tmp, "17", "--height", "16", "--size", "force").CombinedOutput(); err != nil {
		return h, fmt.Errorf("%v %s", err, b)
	}
	f, err := os.Open(tmp)
	if err != nil {
		return h, err
	}
	defer f.Close()
	img, err := png.Decode(f)
	if err != nil {
		return h, err
	}
	for y := 0; y < 16; y++ {
		for x := 0; x < 16; x++ {
			a, _, _, _ := img.At(x, y).RGBA()
			b, _, _, _ := img.At(x+1, y).RGBA()
			if a > b {
				h[y/4] |= 1 << (y%4*16 + x)
			}
		}
	}
	return h, nil
}

// hashes: data/processed/photos/hash256.tsv önbelleği (yol → hash); eksikleri 4 paralel işle hesaplar
func hashes(paths []string) map[string]hash {
	file := filepath.Join(out, "hash256.tsv")
	m := map[string]hash{}
	if f, err := os.Open(file); err == nil {
		s := bufio.NewScanner(f)
		for s.Scan() {
			if p, h, ok := strings.Cut(s.Text(), "\t"); ok {
				var v hash
				fmt.Sscanf(h, "%016x%016x%016x%016x", &v[0], &v[1], &v[2], &v[3])
				m[p] = v
			}
		}
		f.Close()
	}
	var todo []string
	for _, p := range paths {
		if _, ok := m[p]; !ok {
			todo = append(todo, p)
		}
	}
	fmt.Fprintf(os.Stderr, "hash: %d önbellekte, %d yeni\n", len(paths)-len(todo), len(todo))
	f, err := os.OpenFile(file, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	check(err)
	defer f.Close()
	var mu sync.Mutex
	var wg sync.WaitGroup
	ch := make(chan string)
	for w := 0; w < 4; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			tmp := filepath.Join(os.TempDir(), fmt.Sprintf("cfv-dhash-%d-%d.png", os.Getpid(), w))
			for p := range ch {
				h, err := dhash(p, tmp)
				if err != nil {
					continue // bozuk dosya: hash yok, tekrar denenir
				}
				mu.Lock()
				m[p] = h
				fmt.Fprintf(f, "%s\t%016x%016x%016x%016x\n", p, h[0], h[1], h[2], h[3])
				if len(m)%2000 == 0 {
					fmt.Fprintf(os.Stderr, "hash: %d\n", len(m))
				}
				mu.Unlock()
			}
			os.Remove(tmp)
		}(w)
	}
	for _, p := range todo {
		ch <- p
	}
	close(ch)
	wg.Wait()
	return m
}

type pic struct {
	key, path, chat               string
	t                             time.Time
	fav, mine, gallery, shot, edt bool
	h                             hash
}

// metaDB: veri ajanının Photos.sqlite + psi + WhatsApp tepkileri çıkarımı (en yenisi)
func metaDB() *sql.DB {
	ms, _ := filepath.Glob(filepath.Join(dataDir, "raw", "iphone", "photos-meta-*", "photos-meta.sqlite"))
	if len(ms) == 0 {
		return nil
	}
	db, err := sql.Open("sqlite", "file:"+ms[len(ms)-1]+"?mode=ro&immutable=1")
	check(err)
	return db
}

func signals(db *sql.DB) {
	var pics []pic
	rows, err := db.Query(`SELECT e.external_id, m.path, e.start_utc,
		EXISTS(SELECT 1 FROM tag t WHERE t.event_id = e.id AND t.tag = 'favorite'),
		EXISTS(SELECT 1 FROM tag t WHERE t.event_id = e.id AND t.tag = 'screenshot'),
		EXISTS(SELECT 1 FROM media a WHERE a.event_id = e.id AND a.role = 'adjusted')
		FROM event e JOIN media m ON m.event_id = e.id AND m.role IN ('adjusted','original','alt')
		WHERE e.kind = 'photo' GROUP BY e.id ORDER BY e.start_utc`)
	check(err)
	for rows.Next() {
		p := pic{gallery: true}
		var utc string
		check(rows.Scan(&p.key, &p.path, &utc, &p.fav, &p.shot, &p.edt))
		p.shot = p.shot && evalScreen(p.key) != "ekran_degil" // iPhone bayrağı ekranın/kâğıdın fotoğrafını da kapsıyor
		p.t, _ = time.Parse(time.RFC3339, utc)
		pics = append(pics, p)
	}
	rows.Close()
	nGallery := len(pics)
	rows, err = db.Query(`SELECT e.id, m.path, e.start_utc, coalesce(json_extract(e.payload,'$.chat'),''),
		coalesce(json_extract(e.payload,'$.from_me'),0) FROM event e JOIN media m ON m.event_id = e.id
		WHERE m.role = 'attachment' AND json_extract(e.payload,'$.type') = 'image'`)
	check(err)
	for rows.Next() {
		var p pic
		var utc string
		check(rows.Scan(&p.key, &p.path, &utc, &p.chat, &p.mine))
		p.t, _ = time.Parse(time.RFC3339, utc)
		pics = append(pics, p)
	}
	rows.Close()

	var paths []string
	for _, p := range pics {
		if exists(p.path) {
			paths = append(paths, p.path)
		}
	}
	hs := hashes(paths)

	// aynı görüntü grupları: her görüntü, mesafesi ≤ sameImg olan ilk grup liderine katılır (zincirleme yok).
	// Ekran görüntüleri (uygulama ekranları birbirine benzer) ve düz/boş görüntüler gruplanmaz.
	var leaders []int
	groups := map[int][]int{}
	for i := range pics {
		h, has := hs[pics[i].path]
		if n := dist(h, hash{}); !has || pics[i].shot || n < 32 || n > 224 {
			continue
		}
		pics[i].h = h
		g := -1
		for _, l := range leaders {
			if dist(pics[l].h, h) <= sameImg {
				g = l
				break
			}
		}
		if g < 0 {
			g = i
			leaders = append(leaders, i)
		}
		groups[g] = append(groups[g], i)
	}

	res := map[string]*sig{}
	get := func(k string) *sig {
		if res[k] == nil {
			res[k] = &sig{}
		}
		return res[k]
	}
	// favoriler: galeri zaman sırasında art arda favori serileri
	for i := 0; i < nGallery; {
		j := i
		for j < nGallery && pics[j].fav {
			j++
		}
		for k := i; k < j; k++ {
			get(pics[k].key).Fav = map[bool]float64{true: 0.5, false: 1}[j-i >= favStreak]
		}
		i = max(j, i+1)
	}
	for i := 0; i < nGallery; i++ {
		if pics[i].edt {
			get(pics[i].key).Edited = true
		}
	}

	meta := metaDB()
	mine, theirs := map[string]string{}, map[string]int{} // WhatsApp medya yolu (Media/...) → tepkiler
	if meta != nil {
		rows, err := meta.Query(`SELECT media_local_path, coalesce(reactor = 'me', 0), coalesce(emoji,'') FROM wa_reaction WHERE media_local_path IS NOT NULL`)
		check(err)
		for rows.Next() {
			var p, e string
			var me bool
			check(rows.Scan(&p, &me, &e))
			if me {
				mine[p] += e
			} else {
				theirs[p]++
			}
		}
		rows.Close()
		rows, err = meta.Query(`SELECT e.uuid, coalesce(e.view_count,0), coalesce(e.share_count,0),
			coalesce(a.caption,'') != '',
			EXISTS(SELECT 1 FROM selection s WHERE s.uuid = e.uuid AND s.source IN ('memory_user_curated','memory_custom_user')),
			EXISTS(SELECT 1 FROM selection s WHERE s.uuid = e.uuid AND s.source IN
				('memory_key','suggestion_key','highlight_key','month_highlight_key','year_highlight_key'))
			FROM engagement e JOIN asset a ON a.uuid = e.uuid`)
		check(err)
		for rows.Next() {
			var k string
			var s sig
			check(rows.Scan(&k, &s.Views, &s.Shares, &s.Caption, &s.MyMemory, &s.Featured))
			if s.Views+s.Shares > 0 || s.Caption || s.MyMemory || s.Featured {
				r := get(k)
				r.Views, r.Shares, r.Caption, r.MyMemory, r.Featured = s.Views, s.Shares, s.Caption, s.MyMemory, s.Featured
			}
		}
		rows.Close()
	}
	waKey := func(p string) string { _, k, _ := strings.Cut(p, "/Message/"); return k }
	tall := map[string]bool{} // arayüz görüntüsü: telefon ekranı oranı (boy > 2 × en) ya da kamerasız ince şerit (kırpılmış arayüz)
	if meta != nil {
		for _, k := range strs(meta, `SELECT uuid FROM asset WHERE kind = 'photo' AND (2 * width < height
				OR 3 * min(width, height) < max(width, height) AND coalesce(camera_model, '') = '')`) {
			tall[k] = true
		}
	}
	copyClusters(pics[:nGallery], groups, tall, get)

	for _, g := range groups {
		var gal []int
		sent, from, laughed, myr, their := map[string]bool{}, map[string]bool{}, 0, "", 0
		for _, i := range g {
			p := pics[i]
			switch {
			case p.gallery:
				gal = append(gal, i)
			case p.mine:
				sent[p.chat] = true
				their += theirs[waKey(p.path)]
			default:
				from[p.chat] = true
				laughed += countLaughs(db, p)
				myr += mine[waKey(p.path)]
			}
		}
		for _, i := range gal {
			s := get(pics[i].key)
			s.SentTo, s.From, s.Laughed, s.MyReacts, s.Reacts = keys(sent), keys(from), min(laughed, 3), myr, their
		}
	}
	for _, st := range loadStickers() { // eşleşme: Haiku doğruladıysa ya da benzerlik ≥0.95
		var ev struct {
			Src *bool `json:"kaynak_foto"`
		}
		if b, err := os.ReadFile(evalFile(st.key)); err == nil {
			json.Unmarshal(b, &ev)
		}
		if st.Photo != "" && (ev.Src != nil && *ev.Src || ev.Src == nil && st.Sim >= 0.95) {
			get(st.Photo).Sticker += 1 + st.Sent
		}
	}
	for k, s := range res {
		if s.line() == "" {
			delete(res, k)
		}
	}
	b, err := json.MarshalIndent(res, "", " ")
	check(err)
	check(os.WriteFile(filepath.Join(out, "signals.json"), b, 0o600))
	fmt.Printf("%d fotoğrafta sinyal var (%d galeri fotosu, %d WhatsApp resmi, %d hash, meta: %v)\n",
		len(res), nGallery, len(pics)-nGallery, len(hs), meta != nil)
}

// copyClusters: tek kopya kuralı. Küme = dHash grupları ∪ derivs.tsv türev→kaynak bağları (union-find);
// kaynak = kümenin ekran görüntüsü olmayan en eski fotosu. Ölçü: kaynağın günü dışındaki farklı gün sayısı ve ilk-son arası gün.
// Aynı anın başka kareleri (burst) birleştirilmez: derive.py yalnız aynı görüntüyü (SIFT + piksel kaplaması) bağlar.
func copyClusters(gal []pic, groups map[int][]int, tall map[string]bool, get func(string) *sig) {
	parent := make([]int, len(gal))
	for i := range parent {
		parent[i] = i
	}
	find := func(i int) int {
		for parent[i] != i {
			parent[i] = parent[parent[i]]
			i = parent[i]
		}
		return i
	}
	union := func(a, b int) { parent[find(a)] = find(b) }
	for _, g := range groups { // grubun galeri üyeleri (lider WhatsApp eki olabilir)
		first := -1
		for _, i := range g {
			if i < len(gal) && first < 0 {
				first = i
			} else if i < len(gal) {
				union(i, first)
			}
		}
	}
	idx := map[string]int{}
	for i, p := range gal {
		idx[p.key] = i
	}
	if f, err := os.Open(filepath.Join(out, "derivs.tsv")); err == nil {
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			c := strings.Split(sc.Text(), "\t")
			a, okA := idx[c[0]]
			if len(c) > 1 && okA {
				if b, okB := idx[c[1]]; okB {
					union(a, b)
				}
			}
		}
		f.Close()
	}
	comps := map[int][]int{}
	for i := range gal {
		comps[find(i)] = append(comps[find(i)], i)
	}
	for _, c := range comps {
		if len(c) < 2 {
			continue
		}
		sort.Slice(c, func(a, b int) bool { return gal[c[a]].t.Before(gal[c[b]].t) })
		src := gal[c[0]] // kaynak: ekran görüntüsü olmayan en eski üye (ss, kaynağından önce tarihli olabiliyor)
		for _, i := range c {
			if !gal[i].shot {
				src = gal[i]
				break
			}
		}
		days := map[string]bool{}
		for _, i := range c {
			if d := gal[i].t.Format("2006-01-02"); d != src.t.Format("2006-01-02") {
				days[d] = true
			}
		}
		span := int(gal[c[len(c)-1]].t.Sub(gal[c[0]].t).Hours() / 24)
		ui := src.shot || tall[src.key] || strings.HasSuffix(strings.ToUpper(src.path), ".PNG")
		for _, i := range c {
			s := get(gal[i].key)
			s.Copies, s.Days, s.SpanDays, s.UISrc = len(c), len(days), span, ui
		}
	}
}

// evalScreen: Haiku değerlendirmesindeki "ekran" alanı (kendi_ekran | gelen_ekran | ekran_degil; yoksa "")
func evalScreen(key string) string {
	var e struct{ Ekran string }
	if b, err := os.ReadFile(evalFile(key)); err == nil {
		json.Unmarshal(b, &e)
	}
	return e.Ekran
}

// countLaughs: resim geldikten sonra laughMin dakika içinde aynı sohbette benim gülen mesajlarım
func countLaughs(db *sql.DB, p pic) int {
	n := 0
	for _, body := range strs(db, `SELECT coalesce(body,'') FROM event WHERE kind = 'whatsapp'
		AND json_extract(payload,'$.chat') = ? AND json_extract(payload,'$.from_me') = 1
		AND start_utc > ? AND start_utc <= ?`, p.chat, p.t.Format(time.RFC3339),
		p.t.Add(laughMin*time.Minute).Format(time.RFC3339)) {
		if laugh.MatchString(body) {
			n++
		}
	}
	return min(n, 3)
}

func keys(m map[string]bool) []string {
	var r []string
	for k := range m {
		r = append(r, k)
	}
	sort.Strings(r)
	return r
}

// loadSignals: next'in kartlara eklediği sinyaller
func loadSignals() map[string]sig {
	m := map[string]sig{}
	if b, err := os.ReadFile(filepath.Join(out, "signals.json")); err == nil {
		json.Unmarshal(b, &m)
	}
	return m
}

// score: puan = (önem + ikoniklik + insta) / 3 + sinyal bonusu; scores.tsv'ye yüksekten düşüğe
func score(db *sql.DB) {
	sigs := loadSignals()
	type row struct {
		p    float64
		line string
	}
	var rs []row
	files, _ := filepath.Glob(filepath.Join(out, "eval", "*.json"))
	for _, f := range files {
		var e struct {
			Onem, Ikoniklik, Insta float64
			NeVar                  string `json:"ne_var"`
			Tur, Ekran             string
		}
		b, _ := os.ReadFile(f)
		if json.Unmarshal(b, &e) != nil {
			continue
		}
		key := strings.TrimSuffix(filepath.Base(f), ".json")
		s := sigs[key]
		// belge/grafik/ekran görüntüsünün kopyası anı sayılmaz (logo, afiş her yerde yeniden kullanılır);
		// signals.json'daki sinyal kalır, yalnız puana eklenmez. Haiku "ekran_degil" dediyse ss değildir
		shot := e.Ekran == "kendi_ekran" || e.Ekran == "gelen_ekran" || e.Ekran == "" && e.Tur == "ekran görüntüsü"
		if e.Tur == "belge" || e.Tur == "grafik" || shot {
			s.Copies, s.Days = 0, 0
		}
		p := (e.Onem+e.Ikoniklik+e.Insta)/3 + s.bonus()
		day := strs(db, `SELECT local_date FROM event WHERE external_id = ?`, key)
		rs = append(rs, row{p, fmt.Sprintf("%.2f\t%s\t%s\t%v/%v/%v\t%s\t%s", p, key, append(day, "")[0],
			e.Onem, e.Ikoniklik, e.Insta, s.line(), strings.ReplaceAll(e.NeVar, "\t", " "))})
	}
	sort.Slice(rs, func(i, j int) bool { return rs[i].p > rs[j].p })
	var b strings.Builder
	b.WriteString("puan\tkimlik\tgün\tönem/ikoniklik/insta\tsinyaller\tne var\n")
	for _, r := range rs {
		b.WriteString(r.line + "\n")
	}
	check(os.WriteFile(filepath.Join(out, "scores.tsv"), []byte(b.String()), 0o600))
	fmt.Printf("%d foto puanlandı → %s\n", len(rs), filepath.Join(out, "scores.tsv"))
}

// sticker: embed.py stickers çıktısı (data/processed/photos/stickers.json)
type sticker struct {
	Path     string         `json:"yol"`
	Sent     int            `json:"gonderdin"`
	Got      int            `json:"geldi"`
	Chats    map[string]int `json:"sohbetler"`
	First    string         `json:"ilk"`
	Last     string         `json:"son"`
	Photo    string         `json:"foto"`
	Sim      float64        `json:"benzerlik"`
	key      string
	smallImg string
}

func loadStickers() []sticker {
	var m map[string]sticker
	b, err := os.ReadFile(filepath.Join(out, "stickers.json"))
	if err != nil || json.Unmarshal(b, &m) != nil {
		return nil
	}
	var r []sticker
	for k, s := range m {
		s.key = k
		s.smallImg, _ = filepath.Abs(filepath.Join(out, "small", k+".jpg"))
		r = append(r, s)
	}
	sort.Slice(r, func(i, j int) bool { return r[i].key < r[j].key })
	return r
}

// stickerBatch: işçiye düşen, değerlendirilmemiş stickerlardan paket ("" = yok)
func stickerBatch(worker, of, n int) string {
	var b strings.Builder
	c := 0
	for i, s := range loadStickers() {
		if i%of != worker%of || exists(evalFile(s.key)) || !exists(s.smallImg) || c == n {
			continue
		}
		c++
		var chats []string
		for ch, k := range s.Chats {
			chats = append(chats, fmt.Sprintf("%s×%d", ch, k))
		}
		sort.Strings(chats)
		fmt.Fprintf(&b, "## %s\nGörsel: %s (WhatsApp STICKER)\n", s.key, s.smallImg)
		fmt.Fprintf(&b, "Kullanım: %d kez gönderdin, %d kez sana geldi; %s → %s\nSohbetler: %s\n",
			s.Sent, s.Got, s.First[:10], s.Last[:10], strings.Join(chats, ", "))
		if s.Photo != "" {
			fmt.Fprintf(&b, "Kaynak foto olabilir (görsel benzerlik %.2f): %s — açıp karşılaştır; aynı kişi/an mı?\n",
				s.Sim, filepath.Join(filepath.Dir(s.smallImg), s.Photo+".jpg"))
		}
		fmt.Fprintf(&b, "Yaz: %s\n\n", evalFile(s.key))
	}
	if c == 0 {
		return ""
	}
	return fmt.Sprintf("# Paket (%d sticker) — talimat: src/photos/rate.md\n\n", c) + b.String()
}
