#!/bin/bash
# iCloud Fotoğraflar → $ICLOUD_DIR (sunucu arşivi). cfv-photos, 2026-11-10.
# İndirme indir.py ile (icloudpd'nin yol fonksiyonları, ama: var olan ada dokunmaz, exif farkı için '-<boyut>'
# kopyası indirmez, boyut tutmadan yerine koymaz, .xmp'yi yalnız yoksa yazar). Ayrıntı: indir.py başlığı.
# Kural: hiçbir şey silmez / üzerine yazmaz.
#
# Kullanım: cfv-icloudpd.sh gunluk     en yeniden geriye, art arda 300 tam öğe görünce durur (her gün)
#           cfv-icloudpd.sh tam        her şeyi tarar (haftalık: sonradan eklenmiş eski tarihli öğeler)
#           cfv-icloudpd.sh tam-video  tüm videoları tarar
# Ortam: ICLOUD_USER (zorunlu; /etc/cfv-icloudpd.env), ortam.sh'teki yollar, ICLOUD_TRIES, ICLOUD_STALL_MIN
# Çıkış: 0 tamam, 2 giriş gerekiyor (Tuna 2FA), 3 denemeler bitti / disk yok, 4 kullanım ya da kilit hatası.
set -u
: "${ICLOUD_USER:?ICLOUD_USER yok}"
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/ortam.sh"
LOG=$ST/indirme.log
MODE=${1:-gunluk}
TRIES=${ICLOUD_TRIES:-30}
STALL_MIN=${ICLOUD_STALL_MIN:-30}

case $MODE in
  gunluk|tam-video|tam) ;;
  *) echo "bilinmeyen kip: $MODE" >&2; exit 4 ;;
esac

mkdir -p "$ST"; touch "$LOG"
exec 9>"$ST/.kilit"
flock -n 9 || { echo "=== başka tur çalışıyor, atlandı ($MODE) $(date '+%F %T') ===" >>"$LOG"; exit 0; }
# Bu çalıştırmanın log'daki başlangıç satırı (kontrol.sh buradan okur)
echo "$(( $(wc -l <"$LOG") + 1 )) $MODE" >"$ST/son-calistirma"
[ -d "$DIR" ] || { echo "=== HATA: $DIR yok (disk bağlı değil?) $(date '+%F %T') ===" >>"$LOG"; exit 3; }

indir() { ICLOUD_DIR="$DIR" ICLOUD_COOKIES="$COOK" ICLOUD_STATE="$ST" "$PY" -I "$HERE/indir.py" "$@" </dev/null >>"$LOG" 2>&1; }

# Takılma: log da klasör de STALL_MIN dk değişmiyorsa (410 döngüsü / asılı bağlantı) turu öldür.
watch_stall() {
  local pid=$1 still=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 60
    if [ -n "$(find "$LOG" -mmin -1 2>/dev/null)" ] || [ -n "$(find "$DIR" -newer "$ST/.nabiz" -print -quit 2>/dev/null)" ]; then
      still=0; touch "$ST/.nabiz"
    else
      still=$((still + 1))
    fi
    if [ "$still" -ge "$STALL_MIN" ]; then
      echo "=== TAKILDI: $STALL_MIN dk hareket yok, tur durduruluyor $(date '+%F %T') ===" >>"$LOG"
      kill -TERM "$pid"; return
    fi
  done
}

for i in $(seq 1 "$TRIES"); do
  start=$(( $(wc -l <"$LOG") + 1 ))
  echo "=== tur $i ($MODE) $(date '+%F %T') ===" >>"$LOG"
  touch "$ST/.nabiz"
  indir "$MODE" &
  pid=$!
  watch_stall "$pid"
  wait "$pid"; rc=$?
  run=$(tail -n +"$start" "$LOG")
  if [ "$rc" -eq 0 ] && ! grep -qE '^[0-9-]+ [0-9:]+ ERROR|Traceback|=== TAKILDI' <<<"$run"; then
    echo "=== TAMAM ($MODE) $(date '+%F %T') ===" >>"$LOG"; exit 0
  fi
  [ "$rc" -eq 4 ] && { echo "=== HATA: kullanım ya da başka indir.py çalışıyor (rc=4) $(date '+%F %T') ===" >>"$LOG"; exit 4; }
  if [ "$rc" -eq 2 ]; then
    # Apple'ın anlık hatası da oturumu geçersiz gösterebilir: 5 dk sonra bir kez daha sına
    sleep 300
    indir --oturum; [ $? -eq 2 ] && { echo "=== GİRİŞ GEREKİYOR $(date '+%F %T') ===" >>"$LOG"; exit 2; }
  fi
  echo "=== tur $i hatalı (rc=$rc), tekrar denenecek $(date '+%F %T') ===" >>"$LOG"
  sleep 120   # 410 (link ~15 dk'da ölür) / ağ kesintisi sonrası yeni listeleme
done
echo "=== $TRIES tur bitti, hata sürüyor ($MODE) $(date '+%F %T') ===" >>"$LOG"
exit 3
