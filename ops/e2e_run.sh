#!/bin/bash
# NX-037: Durchgangstest des Produktversprechens (ops/e2e_promise.py) im Backend-Container ausfuehren.
# Keine echte Mail, kein echter KI-Aufruf; legt einen Test-Mandanten an und loescht ihn wieder.
# Cron: /etc/cron.d/negotiatex-e2e (05:45 UTC, vor dem Tagesbericht). Ergebnis: /var/log/negotiatex-e2e.log
set -u
APP=/root/negotiatex
echo "=== $(date -u '+%Y-%m-%d %H:%M:%S') UTC"
docker cp "$APP/ops/e2e_promise.py" negotiatex-backend:/tmp/e2e_promise.py || { echo "E2E-ERGEBNIS FEHLER (Skript nicht kopierbar)"; exit 1; }
out=$(docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python /tmp/e2e_promise.py 2>&1)
rc=$?
grep -aE '^(OK|FEHLER|HINWEIS|E2E|Dauer)' <<<"$out"
# Ohne Ergebniszeile (Importfehler, Abbruch, Container weg) trotzdem eindeutig als Fehler kennzeichnen
if ! grep -aq '^E2E-ERGEBNIS' <<<"$out"; then
  tail -5 <<<"$out" | sed 's/^/  /'
  echo "E2E-ERGEBNIS FEHLER (Abbruch ohne Ergebnis, rc=$rc)"
  [ "$rc" -eq 0 ] && rc=1
fi
docker exec negotiatex-backend rm -f /tmp/e2e_promise.py
exit "$rc"
