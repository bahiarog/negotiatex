#!/bin/sh
# Wiederherstellungstest: spielt das neueste Backup in einen Wegwerf-Container mit
# dem produktiven DB-Image ein und vergleicht es mit dem Manifest des Backups
# (Zeilen je Tabelle, Dateizahl der Uploads) sowie RLS-Policies, App-Rolle und
# pgvector. Beruehrt die produktive Datenbank nicht. Exit-Code != 0 bei Abweichung.
set -eu

BACKUP_DIR=${BACKUP_DIR:-/root/negotiatex/backups/daily}
IMAGE=${IMAGE:-negotiatex-postgres:16-pgvector0.8.0}
NAME=negotiatex-restore-test
TS=$(ls -1 "$BACKUP_DIR"/negotiatex_*.dump | sed 's/.*negotiatex_\(.*\)\.dump/\1/' | sort | tail -1)
[ -n "$TS" ] || { echo "kein Backup gefunden"; exit 2; }

( cd "$BACKUP_DIR" && sha256sum -c "checksums_$TS.sha256" >/dev/null ) || { echo "Pruefsumme falsch fuer $TS"; exit 3; }

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -e POSTGRES_PASSWORD="$(head -c 24 /dev/urandom | base64)" "$IMAGE" >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
i=0; until docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 60 ] && exit 4; sleep 1; done
sleep 2

docker exec -i "$NAME" psql -U postgres -q -v ON_ERROR_STOP=0 < "$BACKUP_DIR/globals_$TS.sql" 2>&1 | grep -v "already exists" || true
docker exec -i "$NAME" pg_restore -U postgres -C -d postgres < "$BACKUP_DIR/negotiatex_$TS.dump"

docker exec "$NAME" psql -U postgres -d negotiatex -tA -c "
  SELECT string_agg(format('%s=%s', t, (xpath('/row/c/text()',
         query_to_xml(format('SELECT count(*) AS c FROM public.%I', t), false, true, '')))[1]::text), E'\n' ORDER BY t)
  FROM (SELECT tablename AS t FROM pg_tables WHERE schemaname = 'public') s;" > /tmp/restore_counts_$TS.txt

FAIL=0
grep -v '^upload_files=' "$BACKUP_DIR/manifest_$TS.txt" | while IFS='=' read -r t n; do
  r=$(grep "^$t=" /tmp/restore_counts_$TS.txt | cut -d= -f2)
  [ "$r" = "$n" ] || echo "ABWEICHUNG $t: Manifest $n, wiederhergestellt ${r:-fehlt}"
done > /tmp/restore_diff_$TS.txt
[ -s /tmp/restore_diff_$TS.txt ] && { cat /tmp/restore_diff_$TS.txt; FAIL=1; }

TABLES=$(wc -l < /tmp/restore_counts_$TS.txt)
POLICIES=$(docker exec "$NAME" psql -U postgres -d negotiatex -tAc "SELECT count(*) FROM pg_policies")
APPROLE=$(docker exec "$NAME" psql -U postgres -d negotiatex -tAc "SELECT count(*) FROM pg_roles WHERE rolname='negotiatex_app' AND NOT rolbypassrls")
VECTORS=$(docker exec "$NAME" psql -U postgres -d negotiatex -tAc "SELECT count(*) FROM mdc_retrieval_chunks WHERE embedding IS NOT NULL")
[ "$APPROLE" = "1" ] || { echo "App-Rolle fehlt oder umgeht RLS"; FAIL=1; }

EXPECTED_FILES=$(grep '^upload_files=' "$BACKUP_DIR/manifest_$TS.txt" | cut -d= -f2)
ARCHIVE_FILES=$(tar tzf "$BACKUP_DIR/uploads_$TS.tar.gz" | grep -vc '/$')
[ "$ARCHIVE_FILES" = "$EXPECTED_FILES" ] || { echo "Uploads: Archiv $ARCHIVE_FILES Dateien, erwartet $EXPECTED_FILES"; FAIL=1; }

rm -f /tmp/restore_counts_$TS.txt /tmp/restore_diff_$TS.txt
STATUS=$([ $FAIL -eq 0 ] && echo ok || echo FEHLER)
echo "$(date -Iseconds) restore-test $STATUS backup=$TS tabellen=$TABLES policies=$POLICIES vektoren=$VECTORS uploads=$ARCHIVE_FILES"
exit $FAIL
