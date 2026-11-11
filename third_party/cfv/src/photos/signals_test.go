package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// copyClusters: dHash grubu + türev zinciri tek küme; kaynak ekran görüntüsü olmayan en eski üye;
// arayüz kaynaklı kümeye bonus yok.
func TestCopyClusters(t *testing.T) {
	out = t.TempDir()
	day := func(s string) time.Time { v, _ := time.Parse("2006-01-02", s); return v }
	gal := []pic{
		{key: "A", path: "a.HEIC", t: day("2021-01-01")},            // kaynak
		{key: "B", path: "b.JPG", t: day("2021-01-01")},             // dHash kopyası, aynı gün
		{key: "C", path: "c.PNG", t: day("2020-12-31"), shot: true}, // kaynaktan önce tarihli ss
		{key: "D", path: "d.jpg", t: day("2024-01-01")},             // WhatsApp yeniden kaydı (A'nın türevi)
		{key: "E", path: "e.PNG", t: day("2025-01-01")},             // D'den kesim (zincir)
		{key: "F", path: "f.PNG", t: day("2020-01-01")},             // arayüz kaynağı
		{key: "G", path: "g.PNG", t: day("2022-06-01"), shot: true}, // F'nin ss türevi
		{key: "H", path: "h.HEIC", t: day("2023-01-01")},            // tek başına
	}
	derivs := "turev\tkaynak\ttur\nC\tA\tekran\nD\tA\tkayit\nE\tD\tpng\nG\tF\tekran\nX\tA\tekran\n"
	if err := os.WriteFile(filepath.Join(out, "derivs.tsv"), []byte(derivs), 0o600); err != nil {
		t.Fatal(err)
	}
	res := map[string]*sig{}
	get := func(k string) *sig {
		if res[k] == nil {
			res[k] = &sig{}
		}
		return res[k]
	}
	copyClusters(gal, map[int][]int{0: {0, 1, 99}}, map[string]bool{}, get)

	for _, k := range []string{"A", "B", "C", "D", "E"} {
		s := res[k]
		if s == nil || s.Copies != 5 || s.Days != 3 || s.SpanDays != 1462 || s.UISrc {
			t.Fatalf("%s: %+v (beklenen kopya 5, gün 3, yayılım 1462, arayüz değil)", k, s)
		}
	}
	if b := res["A"].bonus(); b != 1.5 {
		t.Errorf("A bonusu %v, beklenen 1.5", b)
	}
	if s := res["G"]; s == nil || !s.UISrc || s.bonus() != 0 {
		t.Errorf("G: %+v — arayüz kaynaklı kümeye bonus verilmemeli", s)
	}
	if res["H"] != nil {
		t.Errorf("H tek başına, sinyal almamalı: %+v", res["H"])
	}
}
