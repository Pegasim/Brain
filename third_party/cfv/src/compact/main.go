// compact: CFV'nin hiyerarşik recursive sıkıştırması. Ayrıntı: README "Raporlar".
//
//	compact jobs [düğüm]          yazılacak/bayat düğümler, çocuklar önce
//	compact input <düğüm>         o düğümün LLM girdisi (ham veri ya da alt raporlar)
//	compact day <gün> [9-12]      bir günün (ya da saat aralığının) ham dökümü
//	compact send [düğüm]          cfv-editor'a "işleri yap" mesajı (bp)
package main

import (
	"database/sql"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"

	_ "modernc.org/sqlite"
)

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "kullanım: compact jobs|input|day|send [düğüm]")
		os.Exit(2)
	}
	arg := func(i int, def string) string {
		if len(os.Args) > i {
			return os.Args[i]
		}
		return def
	}
	db, err := sql.Open("sqlite", "file:"+filepath.Join(dataDir, "processed", "cfv.sqlite")+"?mode=ro")
	check(err)
	t := load(db)
	switch os.Args[1] {
	case "jobs":
		n, err := t.find(arg(2, ""))
		check(err)
		todo := t.stale(n)
		for _, n := range todo {
			src := "ham veri"
			if len(t.split(n)) > 0 {
				src = "alt raporlar"
			}
			fmt.Printf("%s\t%s\t%s\t%s\n", n.name(), n.level(), src, n.label())
		}
		fmt.Fprintf(os.Stderr, "# %d iş\n", len(todo))
	case "input":
		n, err := t.find(arg(2, ""))
		check(err)
		fmt.Print(t.input(n))
	case "day":
		h0, h1 := 0, 0
		fmt.Sscanf(arg(3, ""), "%d-%d", &h0, &h1)
		fmt.Print(dossier(db, arg(2, ""), h0, h1, false, true))
	case "send":
		msg := "İş var: `./bin/compact jobs " + arg(2, "") + "` listesini sırayla yap (src/compact/editor.md'yi oku)."
		out, err := exec.Command("bp", "msg", "cfv-editor", msg).CombinedOutput()
		fmt.Print(string(out))
		check(err)
	default:
		check(fmt.Errorf("bilinmeyen komut: %s", os.Args[1]))
	}
}
