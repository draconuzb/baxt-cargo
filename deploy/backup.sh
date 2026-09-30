#!/usr/bin/env bash
# Kuniga bir marta bazani zaxiralash (TZ 5.8.5).
#
# Nega oddiy `cp` emas: baza ishlab turganda nusxa olinsa fayl yarim
# yozilgan holatda tushishi mumkin. `sqlite3 .backup` buni to'g'ri qiladi.
#
# Cron ga qo'yish:
#   0 3 * * * /opt/baxt_cargo/deploy/backup.sh >> /var/log/baxt-backup.log 2>&1

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/baxt_cargo}"
DB_PATH="${DB_PATH:-$APP_DIR/cargo.db}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M)"
OUT="$BACKUP_DIR/cargo-$STAMP.db"

if [ ! -f "$DB_PATH" ]; then
    echo "$(date '+%F %T') XATO: $DB_PATH topilmadi"
    exit 1
fi

# Ishlab turgan bazadan xavfsiz nusxa
sqlite3 "$DB_PATH" ".backup '$OUT'"
gzip -f "$OUT"

# Eski nusxalarni tozalash
find "$BACKUP_DIR" -name 'cargo-*.db.gz' -mtime "+$KEEP_DAYS" -delete

echo "$(date '+%F %T') zaxira tayyor: $OUT.gz ($(du -h "$OUT.gz" | cut -f1))"

# Eslatma: .env va *.session fayllarini ham alohida, xavfsiz joyda
# saqlang — ular yo'qolsa Telegram akkauntga qayta kirish kerak bo'ladi.
