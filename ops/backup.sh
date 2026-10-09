#!/bin/sh
# Taegliches Backup von negotiatex: Datenbank (Rollen + Daten) und Upload-Volume
# (Originaldokumente). Schreibt ein Manifest mit Zeilenzahlen und Dateizahl, gegen
# das ops/restore_test.sh die Wiederherstellung prueft.
# Cron: siehe ops/README im Betriebshandbuch (docs/MDC_Betrieb.md).
set -eu

BACKUP_DIR=${BACKUP_DIR:-/root/negotiatex/backups/daily}
RETENTION_DAYS=${RETENTION_DAYS:-14}
TS=$(date +%Y%m%d_%H%M%S)
umask 077
mkdir -p "$BACKUP_DIR"

docker exec negotiatex-db pg_dumpall -U negotiatex --globals-only > "$BACKUP_DIR/globals_$TS.sql"
docker exec negotiatex-db pg_dump -U negotiatex -Fc negotiatex > "$BACKUP_DIR/negotiatex_$TS.dump"
docker run --rm -v negotiatex_uploads:/data:ro -v "$BACKUP_DIR":/backup alpine \
  tar czf "/backup/uploads_$TS.tar.gz" -C /data .
chmod 600 "$BACKUP_DIR/uploads_$TS.tar.gz"  # im Container erzeugt, umask greift dort nicht
chmod 700 "$BACKUP_DIR"

# Manifest: exakte Zeilenzahl je Tabelle direkt nach dem Dump (bei Schreiblast
# sind kleine Abweichungen zum Dump-Schnappschuss moeglich und im Test markiert).
docker exec negotiatex-db psql -U negotiatex -d negotiatex -tA -c "
  SELECT string_agg(format('%s=%s', t, (xpath('/row/c/text()',
         query_to_xml(format('SELECT count(*) AS c FROM public.%I', t), false, true, '')))[1]::text), E'\n' ORDER BY t)
  FROM (SELECT tablename AS t FROM pg_tables WHERE schemaname = 'public') s;" > "$BACKUP_DIR/manifest_$TS.txt"
echo "upload_files=$(docker run --rm -v negotiatex_uploads:/data:ro alpine find /data -type f | wc -l)" >> "$BACKUP_DIR/manifest_$TS.txt"

( cd "$BACKUP_DIR" && sha256sum "globals_$TS.sql" "negotiatex_$TS.dump" "uploads_$TS.tar.gz" "manifest_$TS.txt" > "checksums_$TS.sha256" )
find "$BACKUP_DIR" -type f -mtime +"$RETENTION_DAYS" -delete

echo "$(date -Iseconds) backup ok $TS $(du -ch "$BACKUP_DIR"/*_"$TS".* | tail -1 | cut -f1)"
