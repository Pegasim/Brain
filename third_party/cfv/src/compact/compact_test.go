package main

// SENTETİK veri: sınır küçültülerek bölünme ve yeni verinin yalnız üst düğümleri bayatlatması.

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func testDB(t *testing.T) *sql.DB {
	dataDir, Limit = t.TempDir(), 4000
	db, err := sql.Open("sqlite", filepath.Join(dataDir, "t.sqlite"))
	check(err)
	schema, err := os.ReadFile("../cfv/schema.sql")
	check(err)
	_, err = db.Exec(string(schema) + `INSERT INTO source (id, kind, raw_path, imported_at, importer) VALUES (1, 't', 't', '', '')`)
	check(err)
	add(db, "a", "note", "2020-05-01T06:00:00Z", "az veri")
	for h := 3; h < 21; h++ { // 2025-03-10: her saatte uzun mesajlar → gün sığmaz
		for i := 0; i < 8; i++ {
			add(db, fmt.Sprintf("m%d-%d", h, i), "whatsapp", fmt.Sprintf("2025-03-10T%02d:%02d:00Z", h, i), strings.Repeat("x", 200))
		}
	}
	add(db, "n", "note", "2025-03-20T06:00:00Z", "sakin gün")
	return db
}

func add(db *sql.DB, id, kind, utc, body string) {
	_, err := db.Exec(`INSERT INTO event (source_id, external_id, kind, start_utc, tz_offset_min, local_date, title, body)
		VALUES (1, ?, ?, ?, 180, ?, 'x', ?)`, id, kind, utc, localTime(utc, 180).Format("2006-01-02"), body)
	check(err)
}

func names(ns []node) []string {
	var out []string
	for _, n := range ns {
		out = append(out, n.name())
	}
	return out
}

func TestSplitOnlyWhereNeeded(t *testing.T) {
	tr := load(testDB(t))
	got := strings.Join(names(tr.stale(tr.root())), " ")
	for _, want := range []string{"2020 ", "2025/03/W11/10/", "2025/03/W11/10 ", "2025/03/W12 ", "kök"} {
		if !strings.Contains(got+" ", want) {
			t.Errorf("%q yok: %s", want, got)
		}
	}
	if strings.Contains(got, "2025/03/W12/20") { // sakin gün kendi haftasında yaprak
		t.Errorf("sakin gün bölünmemeli: %s", got)
	}
	if !strings.HasSuffix(got, "kök") || strings.Index(got, "2025/03/W11/10/") > strings.Index(got, "2025/03/W11/10 ") {
		t.Errorf("çocuklar önce gelmeli: %s", got)
	}
}

func TestNewDataRestalesAncestorsOnly(t *testing.T) {
	db := testDB(t)
	tr := load(db)
	tr.walk(tr.root(), func(n node) {
		os.MkdirAll(filepath.Dir(n.file()), 0o700)
		os.WriteFile(n.file(), []byte(fmt.Sprintf("---\nrange: %s\nsig: %s\n---\n", n.label(), tr.sig(n))), 0o600)
	})
	if s := load(db).stale(tr.root()); len(s) != 0 {
		t.Fatalf("hepsi güncel olmalı: %v", names(s))
	}
	add(db, "yeni", "note", "2020-06-01T06:00:00Z", "eklendi")
	tr = load(db)
	if got := strings.Join(names(tr.stale(tr.root())), " "); got != "2020 kök" {
		t.Errorf("yalnız 2020 ve kök bayatlamalı: %s", got)
	}
	if in := tr.input(tr.root()); !strings.Contains(in, "ALT RAPORLAR (2 yıl") {
		t.Errorf("kök girdisi alt raporlardan olmalı:\n%s", in)
	}
}
