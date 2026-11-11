#!/bin/bash
# iCloud oturumunu yeniler (Tuna çalıştırır; ~2 ayda bir). Apple şifresi + telefona gelen 2FA kodu sorulur.
# Şifre saklanmaz; yalnız oturum çerezi tunapro'nun çerez dizinine yazılır.
set -eu
[ -r /etc/cfv-icloudpd.env ] && . /etc/cfv-icloudpd.env
: "${ICLOUD_USER:?ICLOUD_USER yok (/etc/cfv-icloudpd.env)}"
. "$(dirname "$0")/ortam.sh"
# Not: icloudpd kullanıcı adını yalnız --username ile alır (ps'te görünür); bu yalnız Tuna'nın elle girişinde,
# zamanlanmış turlar oturumu indir.py --oturum ile süreç içinden sınar.
exec sudo -u tunapro /opt/icloudpd/venv/bin/icloudpd --auth-only --no-progress-bar \
  --cookie-directory "$COOK" --username "$ICLOUD_USER"
