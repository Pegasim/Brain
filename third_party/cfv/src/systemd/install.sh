#!/bin/sh
# cfv systemd --user birimlerini bağla ve timer'ı başlat (dosyalar repo'da kalır).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
for u in "$here"/cfv-*.service "$here"/cfv-*.timer; do
    systemctl --user link "$u"
done
systemctl --user daemon-reload
systemctl --user enable --now cfv-gcal-sync.timer cfv-day-report.timer
systemctl --user list-timers 'cfv-*'
