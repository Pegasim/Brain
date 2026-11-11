#!/usr/bin/env bash
# Sıcaklık bekçisi (root gerekmez): CPU ≥ 90°C ya da GPU ≥ 80°C olunca ağır işleri dondurur
# (SIGSTOP), CPU < 80°C ve GPU < 72°C olunca kaldıkları yerden sürdürür (SIGCONT).
# Çalıştır: systemd-run --user --unit=cfv-thermal bash src/tools/thermal-guard.sh
PAT=${CFV_HEAVY:-'src/predict/\.venv/bin/python|ollama runner|llama-server'}
LOG=${CFV_DATA:-$(dirname "$0")/../../data}/logs/thermal.log
paused=0
while sleep 5; do
  c=$(sensors | awk '/Package id 0/ {gsub(/[^0-9.]/, "", $4); print int($4)}')
  g=$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader 2>/dev/null || echo 0)
  if (( c >= 90 || g >= 80 )); then
    pkill -STOP -f "$PAT" && (( !paused )) && echo "$(date +%F\ %T) durdur cpu=$c gpu=$g" >> "$LOG"
    paused=1
  elif (( paused && c < 80 && g < 72 )); then
    pkill -CONT -f "$PAT"; paused=0
    echo "$(date +%F\ %T) sürdür cpu=$c gpu=$g" >> "$LOG"
  fi
done
