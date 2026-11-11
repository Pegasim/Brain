# Ortak varsayılanlar (bash betikleri source eder; Python karşılığı icloud_yol.py başında).
# Ortamda verilen değer önceliklidir.
: "${ICLOUD_DIR:=/mnt/depo1/foto/icloud-sync}"
: "${ICLOUD_STATE:=/home/tunapro/.local/state/cfv-icloudpd}"
: "${ICLOUD_COOKIES:=/home/tunapro/.config/icloudpd}"
: "${ICLOUD_PY:=/opt/icloudpd/lib-venv/bin/python}"
: "${CFV_DURUM:=/mnt/depo1/foto/_durum}"
: "${CFV_IKINCI:=/mnt/depo4/foto-ikinci/icloud-sync}"
: "${IKINCI_MIN_GB:=20}"
: "${BP:=$(command -v bp || echo /usr/local/bin/bp)}"
DIR=$ICLOUD_DIR ST=$ICLOUD_STATE COOK=$ICLOUD_COOKIES PY=$ICLOUD_PY DURUM=$CFV_DURUM HEDEF=$CFV_IKINCI
