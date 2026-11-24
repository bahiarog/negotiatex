#!/bin/bash
# Taeglicher Betriebsbericht NegotiateX -> E-Mail an REPORT_TO (Cron: /etc/cron.d/negotiatex-daily-check).
# Nur lesend. Betreff "OK" oder "WARNUNG (n)". Manuell: bash /root/negotiatex/ops/daily_check.sh --print
set -u
MODE="${1:-}"   # vor allen "set --" sichern
REPORT_TO="${REPORT_TO:-bahiarog@me.com}"
warn=0; out=""
line() { out+="$1"$'\n'; }
check() { # check <ok|warn> <text>
  if [ "$1" = ok ]; then line "  ok      $2"; else line "  WARNUNG $2"; warn=$((warn + 1)); fi
}
code() { curl -s -m 20 -o /dev/null -w '%{http_code}' "$1"; }

line "NegotiateX Tagesbericht $(date -u '+%d.%m.%Y %H:%M') UTC"
line ""
line "Erreichbarkeit"
for spec in "https://negotiatex.ai/api/health 200" "https://negotiatex.ai/ 200" "https://negotiatex.ai/uebersicht 200" \
            "https://negotiatex.ai/api/v1/me/access 401" "https://iffiu.com/ 200" "https://kollevio.com/ 200" "https://stuhlsohle.de/ 200"; do
  set -- $spec; got=$(code "$1"); [ "$got" = "$2" ] && check ok "$1 -> $got" || check warn "$1 -> $got (erwartet $2)"
done

line ""
line "Container"
for c in negotiatex-backend negotiatex-db negotiatex-redis iffiu-nginx; do
  st=$(docker inspect -f '{{.State.Status}} seit {{.State.StartedAt}} Neustarts={{.RestartCount}}' "$c" 2>/dev/null || echo fehlt)
  [[ "$st" == running* ]] && check ok "$c: ${st:0:60}" || check warn "$c: $st"
done

line ""
line "Backup (03:15)"
last_b=$(grep -a "backup ok" /var/log/negotiatex-backup.log 2>/dev/null | tail -1)
last_r=$(grep -a "restore-test" /var/log/negotiatex-backup.log 2>/dev/null | tail -1)
if [ -n "$last_b" ] && [ $(( $(date +%s) - $(stat -c %Y /var/log/negotiatex-backup.log) )) -lt 93600 ]; then check ok "${last_b:0:90}"; else check warn "kein Backup in den letzten 26 h"; fi
[[ "$last_r" == *"restore-test ok"* ]] && check ok "${last_r:0:90}" || check warn "Wiederherstellungstest: ${last_r:0:90}"
check warn "Backups liegen nur auf diesem Server (externes Ziel fehlt)"

line ""
line "Anwendung (letzte 24 h)"
logs=$(docker logs --since 24h negotiatex-backend 2>&1)
tb=$(grep -c Traceback <<<"$logs"); [ "$tb" -eq 0 ] && check ok "Fehler (Tracebacks): 0" || check warn "Fehler (Tracebacks): $tb -- zuletzt: $(grep -A30 Traceback <<<"$logs" | grep -E '^[A-Za-z_.]+(Error|Exception)' | tail -1 | cut -c1-120)"
mf=$(grep -c "Email send failed" <<<"$logs"); [ "$mf" -eq 0 ] && check ok "Mailversand: keine Fehler" || check warn "Mailversand: $mf Fehler -- $(grep 'Email send failed' <<<"$logs" | tail -1 | cut -c1-120)"
un=$(grep -c "konnte keinem Fall" <<<"$logs"); line "  info    Eingehende Mails ohne Zuordnung: $un"
ai=$(docker exec negotiatex-backend python -c "
import anthropic
try:
    anthropic.Anthropic().models.list(limit=1); print('gueltig')
except Exception as e: print('UNGUELTIG', type(e).__name__)" 2>/dev/null | tail -1)
[ "$ai" = gueltig ] && check ok "KI-Schluessel gueltig" || check warn "KI-Schluessel: $ai"
runs=$(docker exec negotiatex-db psql -U negotiatex -d negotiatex -At -c \
  "select count(*), coalesce(sum(case when status='failed' then 1 else 0 end),0), coalesce(sum(input_tokens+output_tokens),0) from agent_runs where started_at > now() - interval '24 hours'" 2>/dev/null)
IFS='|' read -r n failed tok <<<"$runs"
[ "${failed:-0}" -eq 0 ] && check ok "Digitales Team: ${n:-0} Arbeitsschritte, ${tok:-0} Tokens" || check warn "Digitales Team: $failed von $n Arbeitsschritten fehlgeschlagen"
open=$(docker exec negotiatex-db psql -U negotiatex -d negotiatex -At -c \
  "select (select count(*) from outreach_actions where status='draft') + (select count(*) from rfq_actions where status='draft') + (select count(*) from negotiation_actions where status='draft')" 2>/dev/null)
line "  info    Entwuerfe, die auf Freigabe warten: ${open:-?}"

line ""
line "Versprechen (Durchgangstest 05:45, NX-037)"
E2E_LOG=/var/log/negotiatex-e2e.log
e2e=$(grep -a "E2E-ERGEBNIS" "$E2E_LOG" 2>/dev/null | tail -1)
if [ -z "$e2e" ] || [ $(( $(date +%s) - $(stat -c %Y "$E2E_LOG" 2>/dev/null || echo 0) )) -ge 93600 ]; then
  check warn "kein Durchgangstest in den letzten 26 h"
elif [[ "$e2e" == *"E2E-ERGEBNIS OK"* ]]; then
  check ok "${e2e:0:90}"
else
  check warn "${e2e:0:90}"
  while IFS= read -r f; do line "          ${f:0:110}"; done < <(tac "$E2E_LOG" | awk '/^=== /{exit} {print}' | grep -a '^FEHLER' | head -5)
fi

line ""
line "Server"
disk=$(df --output=pcent / | tail -1 | tr -dc 0-9); [ "$disk" -lt 80 ] && check ok "Festplatte ${disk} % belegt" || check warn "Festplatte ${disk} % belegt"
mem=$(free -m | awk '/Mem:/{printf "%d", $7/$2*100}'); [ "$mem" -gt 10 ] && check ok "Arbeitsspeicher ${mem} % frei" || check warn "Arbeitsspeicher nur ${mem} % frei"
days=$(python3 -c "
import ssl, socket, datetime
c = ssl.create_default_context().wrap_socket(socket.socket(), server_hostname='negotiatex.ai'); c.settimeout(15)
c.connect(('negotiatex.ai', 443)); exp = datetime.datetime.strptime(c.getpeercert()['notAfter'], '%b %d %H:%M:%S %Y %Z')
print((exp - datetime.datetime.utcnow()).days)" 2>/dev/null || echo "?")
[ "$days" != "?" ] && [ "$days" -gt 14 ] && check ok "TLS-Zertifikat noch $days Tage gueltig" || check warn "TLS-Zertifikat: noch $days Tage"
ips=$(journalctl -u ssh --since "24 hours ago" 2>/dev/null | grep -a "Accepted" | awk '{for(i=1;i<=NF;i++) if($i=="from") print $(i+1)}' | sort | uniq -c | awk '{printf "%s (%s) ", $2, $1}')
[ -z "$ips" ] && ips=$(grep -a "Accepted" /var/log/auth.log 2>/dev/null | awk -v d="$(date -d '24 hours ago' +%FT%T)" '$1 > d {for(i=1;i<=NF;i++) if($i=="from") print $(i+1)}' | sort | uniq -c | awk '{printf "%s (%s) ", $2, $1}')
line "  info    SSH-Anmeldungen root (24 h): ${ips:-keine}"
pw=$(sshd -T 2>/dev/null | awk '/^passwordauthentication/{print $2}')
[ "$pw" = no ] && check ok "SSH nur per Schluessel" || check warn "SSH erlaubt noch Passwort-Anmeldung"

status="OK"; [ "$warn" -gt 0 ] && status="WARNUNG ($warn)"
subject="[NegotiateX] Tagesbericht: $status"
if [ "$MODE" = "--print" ]; then echo "$subject"; echo; echo "$out"; exit 0; fi
printf '%s' "$out" | docker exec -i negotiatex-backend python -c "
import sys
from services.email_sender import send_negotiation_email
r = send_negotiation_email(sys.argv[1], sys.argv[2], sys.stdin.read(), from_name='NegotiateX Betrieb')
print('gesendet' if r.get('sent') else 'NICHT gesendet: ' + str(r.get('message')))" "$REPORT_TO" "$subject" 2>&1 | tail -1
