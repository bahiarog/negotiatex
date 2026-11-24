#!/bin/bash
# NX-037: Durchgangstest des Produktversprechens (ops/e2e_promise.py) im Backend-Container ausfuehren.
# Keine echte Mail, kein echter KI-Aufruf; legt einen Test-Mandanten an und loescht ihn wieder.
# Cron: /etc/cron.d/negotiatex-e2e (05:45 UTC, vor dem Tagesbericht). Ergebnis: /var/log/negotiatex-e2e.log
set -u
APP=/root/negotiatex
echo "=== $(date -u '+%Y-%m-%d %H:%M:%S') UTC"
docker cp "$APP/ops/e2e_promise.py" negotiatex-backend:/tmp/e2e_promise.py || { echo "E2E-ERGEBNIS FEHLER (Skript nicht kopierbar)"; exit 1; }
docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python /tmp/e2e_promise.py 2>&1 | grep -E '^(OK|FEHLER|HINWEIS|E2E|Dauer)'
rc=${PIPESTATUS[0]}
docker exec negotiatex-backend rm -f /tmp/e2e_promise.py
exit "$rc"
