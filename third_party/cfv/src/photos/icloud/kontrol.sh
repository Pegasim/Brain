#!/bin/bash
# cfv-icloudpd turu bitince çalışır (systemd ExecStopPost=+, root). Salt okur; arşive yazmaz.
# Günlük satırı → $DURUM/gunluk.tsv; sorun varsa bp ile haber verir.
# Ortam: ortam.sh'teki yollar, CFV_ALARM_TO (varsayılan cfv-photos), CFV_DENEME=1 (mesaja DENEME öneki)
set -u
# ExecStopPost=+ root koşar ama systemd User=tunapro'nun USER/LOGNAME'ini bırakır → bp "tunapro" diye imzalar ve
# BP_OWNER sınırı uygulanmaz. Root'ken bunları kaldır: imza [service:<birim>] olur (bp whoami ile sınandı).
# HOME de tunapro'nunki kalır; bp hedefin durumunu yanlış dizinde arayıp kuyrukta takılıyordu.
[ "$(id -u)" -eq 0 ] && { unset USER LOGNAME; export HOME=/root; }
. "$(dirname "$0")/ortam.sh"
TO=${CFV_ALARM_TO:-cfv-photos}
LOG=$ST/indirme.log
TSV=$DURUM/gunluk.tsv

read -r start mode <"$ST/son-calistirma" 2>/dev/null || { start=1; mode=?; }
run=$(tail -n +"$start" "$LOG" 2>/dev/null)
son=$(grep -E '^=== ' <<<"$run" | tail -1)
case "$son" in
  *TAMAM*) sonuc=tamam ;;
  *"başka tur"*) sonuc=atlandi ;;
  *GİRİŞ*) sonuc=giris-gerekiyor ;;
  *) sonuc="hata(${EXIT_STATUS:-?})" ;;
esac
indirilen=$(grep -cE '^[0-9-]+ [0-9:]+ INFO +Downloaded ' <<<"$run")
# (ikinci kopya listesi $ST/indirilenler.txt'yi indir.py her yerleşen dosyada kendisi yazar)

mkdir -p "$DURUM"
# Medyasız .xmp (kuru tur / eksik dosya için icloudpd'nin bıraktığı) ayrı sayılır ve listelenir; silinmez.
ORPHANS=$DURUM/medyasiz-xmp.txt
read -r medya xmp yarim bayt enyeni orphan < <(find "$DIR" -type f -printf '%T@ %s %p\n' 2>/dev/null | awk -v out="$ORPHANS.yeni" '
  { t=$1; s=$2; p=substr($0, index($0, $3)); b+=s
    if (p ~ /\.xmp$/) { x++; X[substr(p, 1, length(p)-4)]=1 }
    else if (p ~ /(\.part|-part)$/) y++
    else { m++; M[p]=1; if (t>mx) mx=t } }
  END { for (k in X) if (!(k in M)) { o++; print k ".xmp" > out }
        printf "%d %d %d %d %d %d\n", m, x, y, b, mx, o }')
[ -f "$ORPHANS.yeni" ] && sort "$ORPHANS.yeni" >"$ORPHANS" && rm -f "$ORPHANS.yeni" || : >"$ORPHANS"
enyeni_tarih=$(date -d "@${enyeni%.*}" '+%F' 2>/dev/null || echo ?)

[ -s "$TSV" ] || printf 'zaman\tkip\tsonuc\tindirilen\tmedya\txmp\tmedyasiz_xmp\tyarim\tbayt\ten_yeni_cekim\n' >"$TSV"
onceki=$(tail -n +2 "$TSV" | tail -1 | cut -f5)
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(date '+%F %T')" "$mode" "$sonuc" "$indirilen" \
  "$medya" "$xmp" "$orphan" "$yarim" "$bayt" "$enyeni_tarih" >>"$TSV"

sorun=()
[ "$sonuc" = tamam ] || [ "$sonuc" = atlandi ] || sorun+=("tur sonucu: $sonuc (son: ${son:-yok})")
[ -n "$onceki" ] && [ "$medya" -lt "$onceki" ] && sorun+=("medya sayısı AZALDI: $onceki → $medya (biri sildi mi?)")
[ "$yarim" -gt 0 ] && sorun+=("$yarim yarım dosya (.part/.cfv-part/.kv-part) duruyor")
# '-<boyut>' ekli dosya o boyutta değil (indir.py dokunmaz, rapora yazar) → elle bakılmalı
yarimad=$(grep -c '^yarim' "$ST/farkli-$(date +%F).tsv" 2>/dev/null)
[ "${yarimad:-0}" -gt 0 ] && sorun+=("$yarimad '-<boyut>' dosyası beklenen boyutta değil (farkli-$(date +%F).tsv)")
# depo4 ikinci kopya hedefi: haftalık kopyadan ÖNCE haber ver (server-main 2026-11-10: <20G)
if [ -d "$HEDEF" ]; then
  bos4=$(df -BG --output=avail "$HEDEF" | tail -1 | tr -dc 0-9)
  [ "${bos4:-0}" -lt "$IKINCI_MIN_GB" ] && sorun+=("depo4 ikinci kopya diskinde ${bos4}G boş (<${IKINCI_MIN_GB}G)")
fi

if [ ${#sorun[@]} -gt 0 ]; then
  msg="[cfv-icloudpd kontrol]${CFV_DENEME:+ DENEME (sahte sorun, gerçek değil):} $(IFS='; '; echo "${sorun[*]}"). medya=$medya indirilen=$indirilen. Log: $LOG"
  "$BP" msg "$TO" "$msg" >/dev/null 2>&1 || logger -t cfv-icloudpd "$msg"
  case "$msg" in *depo4*) "$BP" msg server-cfv "$msg" >/dev/null 2>&1 ;; esac
  if [ "$sonuc" = giris-gerekiyor ]; then
    "$BP" msg server-cfv "iCloud oturumu düştü: Tuna'nın yeniden giriş yapması (2FA) gerekiyor. Komut: /srv/central-finite-vault/src/photos/icloud/giris.sh" >/dev/null 2>&1
    command -v laptop-notify >/dev/null && laptop-notify "iCloud girişi gerekiyor" "Sunucu foto çekimi durdu: giris.sh ile 2FA gir" critical
  fi
fi
exit 0
