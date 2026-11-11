#!/bin/sh
# Ham dosyanın tarihli kopyası (yalnız ekleme): snapshot.sh <dosya> <ad-öneki>
# <dosya>, history/ içindeki en yeni kopyadan farklıysa history/<önek>-YYYY-MM-DDTHHMMSS.<uzantı>.zst yazılır.
# Hiçbir şey silinmez, üzerine yazılmaz. systemd ExecStartPre= ile senkrondan ÖNCE çalışır.
set -eu
f=$1; prefix=$2
[ -f "$f" ] || exit 0
dir=$(dirname "$f")/history
ext=${f##*/}; ext=${ext#*.}
umask 077
mkdir -p "$dir"
last=$(ls -1 "$dir"/"$prefix"-*.zst 2>/dev/null | sort | tail -n 1 || true)
if [ -n "$last" ] && zstd -dcq "$last" | cmp -s - "$f"; then
    exit 0
fi
out="$dir/$prefix-$(date +%Y-%m-%dT%H%M%S).$ext.zst"
[ -e "$out" ] && exit 0
zstd -q -19 "$f" -o "$out"
