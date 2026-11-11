#!/bin/bash
# Laptopta OLMAYAN foto/videoların ikinci kopyası (borg /mnt/depo1/foto'yu almıyor; depo1 tek disk).
# Kapsam: (1) >500 MB dosyalar (laptoptan 2026-11-10'da silindi), (2) sunucunun kendi indirdikleri
# (indir.py → indirilenler.txt). Fotoların geri kalanının ikinci kopyası laptopta.
# Kaynağa yazmaz; hedefte silme yok (--delete yok), hedefteki dosyanın üzerine yazma yok (--ignore-existing:
# kaynak bozulursa iyi kopya korunur; farkı aylık doğrulama yakalar). root çalıştırır.
# Hedef /mnt/depo4/arsiv DIŞINDA kalmalı (arsiv borg'a giriyor; server-main 2026-11-10).
# Kullanım: ikinci-kopya.sh          kopya; ayın ilk Pazarı (gün<=7) ardından checksum doğrulaması
#           ikinci-kopya.sh dogrula  yalnız checksum doğrulaması (rsync -anc --itemize, fark=0 olmalı)
# Doğrulama farkı otomatik DÜZELTMEZ: depo1'de bozulan dosya -c ile iyi kopyanın üzerine yazılırdı.
# Fark/az yer (<IKINCI_MIN_GB, 20) → bp msg cfv-photos + server-cfv.
# Çıkış: 0 tamam/kilit dolu, 3 hedef disk yok, 5 kilit dosyası açılamadı, diğer: rsync rc / doğrulama farkı.
set -u
MOD=${1:-kopya}
. "$(dirname "$0")/ortam.sh"
MIN_GB=$IKINCI_MIN_GB
alarm() { "$BP" msg cfv-photos "[ikinci-kopya] $1" >/dev/null 2>&1; "$BP" msg server-cfv "[ikinci-kopya] $1" >/dev/null 2>&1 || logger -t cfv-ikinci "$1"; }
LOG=$ST/ikinci-kopya.log
LISTE=$(mktemp)
trap 'rm -f "$LISTE"' EXIT

mountpoint -q "$(dirname "$(dirname "$HEDEF")")" || { alarm "hedef disk bağlı değil: $HEDEF (kopya yapılmadı)"; exit 3; }
mkdir -p "$ST"
# Kilit: dolu → başka tur çalışıyor (normal, rc 0). Açılamıyor (izin vb.) → hata (rc 5); sessiz geçme.
not() { echo "=== $1 $(date '+%F %T') ===" 2>/dev/null >>"$LOG" || logger -t cfv-ikinci "$1"; echo "ikinci-kopya: $1" >&2; }
{ exec 8>"$ST/.ikinci-kilit"; } 2>/dev/null || { not "kilit dosyası açılamadı: $ST/.ikinci-kilit (kullanıcı $(id -un)); çıkılıyor"; exit 5; }
flock -n 8 || { not "başka bir ikinci-kopya turu çalışıyor (kilit dolu); çıkılıyor"; exit 0; }
mkdir -p "$HEDEF"

{
  cd "$DIR" && find . -type f -size +500M ! -path '*/.*' ! -name '*.xmp' ! -name '*.part' -printf '%P\n'
  [ -f "$ST/indirilenler.txt" ] && sed -n "s#^$DIR/##p" "$ST/indirilenler.txt"
} | sort -u | while IFS= read -r f; do
  [ -f "$DIR/$f" ] || continue
  echo "$f"; [ -f "$DIR/$f.xmp" ] && echo "$f.xmp"
done >"$LISTE"

dogrula() {
  local out; out=$(mktemp)
  echo "=== checksum doğrulama $(wc -l <"$LISTE") dosya $(date '+%F %T') ===" >>"$LOG"
  ionice -c3 nice -n 15 rsync -anc --itemize-changes --files-from="$LISTE" "$DIR/" "$HEDEF/" >"$out" 2>>"$LOG"
  local rc=$? icerik eksik oznitelik
  # itemize: 'YXcstpoguax'. Dosya satırları:
  #   3. sütun 'c' → içerik farkı (asıl alarm)
  #   '+++++++++' → hedefte yok (henüz kopyalanmamış)
  #   diğerleri → yalnız mtime/izin farkı (içerik aynı)
  icerik=$(grep -c '^[<>.]fc' "$out"); eksik=$(grep -c '^[<>c]f+++++++++' "$out")
  oznitelik=$(( $(grep -c '^[<>ch.]f' "$out") - icerik - eksik ))
  cat "$out" >>"$LOG"; rm -f "$out"
  echo "=== doğrulama bitti rc=$rc icerik_farki=$icerik hedefte_yok=$eksik yalniz_oznitelik=$oznitelik $(date '+%F %T') ===" >>"$LOG"
  [ "$rc" -eq 0 ] && [ "$icerik" -eq 0 ] && [ "$eksik" -eq 0 ] ||
    alarm "checksum doğrulaması: rc=$rc içerik_farkı=$icerik hedefte_yok=$eksik yalnız_öznitelik=$oznitelik (düzeltilmedi; ayrıntı $LOG)"
  return $(( rc || icerik || eksik ))
}

if [ "$MOD" = dogrula ]; then dogrula; exit $?; fi

echo "=== ikinci kopya $(wc -l <"$LISTE") dosya → $HEDEF $(date '+%F %T') ===" >>"$LOG"
ionice -c3 nice -n 15 rsync -a --ignore-existing --files-from="$LISTE" --partial-dir=.rsync-yarim --stats "$DIR/" "$HEDEF/" >>"$LOG" 2>&1
rc=$?
echo "=== bitti rc=$rc $(date '+%F %T') ===" >>"$LOG"
[ "$rc" -eq 0 ] || alarm "rsync rc=$rc (ayrıntı $LOG)"
bos=$(df -BG --output=avail "$HEDEF" | tail -1 | tr -dc 0-9)
[ "${bos:-0}" -lt "$MIN_GB" ] && alarm "depo4'te ${bos}G boş kaldı (<${MIN_GB}G); yeni büyük videolar sığmayabilir"
if [ "$rc" -eq 0 ] && [ "$(date +%-d)" -le 7 ]; then dogrula || rc=$?; fi
exit $rc
