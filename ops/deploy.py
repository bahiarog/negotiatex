#!/usr/bin/env python3
"""
Deploy von NegotiateX auf den Server -- immer gleich, mit Sicherung, Pruefung und Ruecksprung.

    python ops/deploy.py            Plan: welche Dateien wuerden sich aendern (aendert nichts)
    python ops/deploy.py --apply    ausfuehren (nur nach Freigabe durch Rafael)

Regeln:
- Deployt wird nur, was committet UND nach origin/main gepusht ist (Server = GitHub-Stand).
- Uebertragen wird der Inhalt aus dem Git-Commit, nicht die Arbeitskopie.
- Vorher werden alle betroffenen Server-Dateien nach /root/negotiatex-deploy/<Zeit>/ gesichert.
- Danach: Backend neu bauen (nur wenn Backend-Dateien betroffen), nginx pruefen und neu laden,
  Health-Checks fuer NegotiateX und die Nachbar-Domains. Schlaegt ein Check fehl, wird
  automatisch auf die Sicherung zurueckgesetzt.
- Browser-Tests (ops/ui_tests.py, NX-004): vor dem Deploy als Ausgangslage -- schlagen sie fehl, wird
  nicht deployt. Nach dem Deploy erneut -- schlagen sie dann fehl, wird zurueckgesetzt.
  "--ohne-ui" ueberspringt beides (nur mit Begruendung, z. B. wenn die Tests selbst kaputt sind).
- Zugang per SSH-Schluessel ~/.ssh/negotiatex_deploy (kein Passwort).
"""
import hashlib
import io
import os
import shlex
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # ops/ui_tests importierbar

HOST = "root@187.124.10.46"
KEY = os.path.expanduser("~/.ssh/negotiatex_deploy")
REMOTE_APP = "/root/negotiatex"
REMOTE_WEB = "/var/www/negotiatex"
NGINX = "iffiu-nginx"
SCOPE = ("app/", "landing/", "Dockerfile", "docker-compose.yml", "ops/")
NEIGHBOURS = ("https://iffiu.com/", "https://kollevio.com/", "https://stuhlsohle.de/")
CHECKS = (("https://negotiatex.ai/api/health", "200"), ("https://negotiatex.ai/", "200"),
          ("https://negotiatex.ai/uebersicht", "200"), ("https://negotiatex.ai/api/v1/me/access", "401"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SSH = ["ssh", "-i", KEY, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", HOST]


def git(*args, binary=False):
    out = subprocess.run(["git", "-C", REPO, *args], capture_output=True, check=True).stdout
    return out if binary else out.decode().strip()


def remote(script, stdin=None, check=True):
    """Fuehrt `script` auf dem Server aus; `stdin` (Bytes) sind Daten fuer das Skript, nicht das Skript selbst."""
    r = subprocess.run(SSH + ["bash -c " + shlex.quote(script)], input=stdin or b"", capture_output=True)
    if check and r.returncode != 0:
        raise SystemExit(f"Server-Befehl fehlgeschlagen ({r.returncode}):\n{r.stdout.decode()}{r.stderr.decode()}")
    return r.stdout.decode()


def target(path):
    """Repo-Pfad -> Zielpfad auf dem Server."""
    return f"{REMOTE_WEB}/{path[len('landing/'):]}" if path.startswith("landing/") else f"{REMOTE_APP}/{path}"


def md5(data: bytes) -> str:
    return hashlib.md5(data.replace(b"\r", b"")).hexdigest()


def preconditions():
    dirty = [ln for ln in git("status", "--porcelain").splitlines()
             if any(ln[3:].startswith(s) for s in SCOPE)]
    if dirty:
        raise SystemExit("Nicht committete Aenderungen im Deploy-Umfang:\n" + "\n".join(dirty))
    git("fetch", "-q", "origin", "main")
    head, origin = git("rev-parse", "HEAD"), git("rev-parse", "origin/main")
    if head != origin:
        raise SystemExit(f"HEAD ({head[:7]}) ist nicht origin/main ({origin[:7]}). Erst pushen, dann deployen.")
    return head


def plan():
    files = [f for f in git("ls-files").splitlines() if any(f.startswith(s) for s in SCOPE)]
    local = {f: git("show", f"HEAD:{f}", binary=True) for f in files}
    # Webseiten liegen im nginx-Container (massgeblich) und als Kopie auf dem Host.
    listing = "\n".join(f"{f}\t{target(f)}" for f in files)
    script = r"""
while IFS=$'\t' read -r f t; do
  if [[ "$f" == landing/* ]]; then h=$(docker exec %s cat "$t" 2>/dev/null | tr -d '\r' | md5sum | cut -c1-32)
     docker exec %s test -e "$t" || h=fehlt
  else [ -e "$t" ] && h=$(tr -d '\r' < "$t" | md5sum | cut -c1-32) || h=fehlt; fi
  printf '%%s\t%%s\n' "$f" "$h"
done
""" % (NGINX, NGINX)
    remote_hash = dict(line.split("\t") for line in remote(script, stdin=(listing + "\n").encode()).splitlines() if "\t" in line)
    return [f for f in files if remote_hash.get(f) != md5(local[f])], local, remote_hash


def checks():
    script = "\n".join(f'echo "{u} $(curl -s -m 20 -o /dev/null -w "%{{http_code}}" {u}) {exp}"' for u, exp in CHECKS)
    script += "\n" + "\n".join(f'echo "{u} $(curl -s -m 20 -o /dev/null -w "%{{http_code}}" {u}) 200"' for u in NEIGHBOURS)
    rows = [ln.split() for ln in remote(script).splitlines()]
    for u, got, exp in rows:
        print(f"  {'ok ' if got == exp else 'FEHLER'} {u} -> {got} (erwartet {exp})")
    return all(got == exp for _, got, exp in rows)


def rebuild_backend():
    out = remote(f"""cd {REMOTE_APP} && docker compose build backend >/tmp/nx-build.log 2>&1 || {{ tail -20 /tmp/nx-build.log; exit 1; }}
docker compose up -d backend >/dev/null 2>&1
for i in $(seq 1 40); do
  docker exec negotiatex-backend python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health',timeout=3)" 2>/dev/null && {{ echo backend-bereit; break; }}
  sleep 3
done
echo "tracebacks=$(docker logs --since 3m negotiatex-backend 2>&1 | grep -c Traceback)"
""", check=False)
    print("  " + out.strip().replace("\n", "\n  "))
    return "backend-bereit" in out and "tracebacks=0" in out


def reload_nginx():
    out = remote(f'docker exec {NGINX} nginx -t 2>&1 | grep -q successful && docker exec {NGINX} nginx -s reload 2>/dev/null && echo nginx-ok',
                 check=False)
    return "nginx-ok" in out


def main():
    apply = "--apply" in sys.argv
    head = preconditions()
    changed, local, _ = plan()
    print(f"Commit {head[:7]} -- {len(changed)} Datei(en) weichen vom Server ab:")
    for f in changed:
        print(f"  {f} -> {target(f)}")
    if not changed:
        print("Server ist auf diesem Stand. Nichts zu tun.")
        return
    if not apply:
        print("\nPlan only. Ausfuehren mit: python ops/deploy.py --apply  (nur nach Freigabe durch Rafael)")
        return

    ui = "--ohne-ui" not in sys.argv
    if ui:
        from ui_tests import run as ui_run
        print("\n0. Browser-Tests vor dem Deploy (Ausgangslage)")
        if not ui_run(label="vor-deploy"):
            raise SystemExit("Browser-Tests schlagen schon VOR dem Deploy fehl -- nicht deployt. "
                             "Erst klaeren (Protokoll oben); nur begruendet mit --ohne-ui deployen.")
    else:
        print("\nBrowser-Tests uebersprungen (--ohne-ui).")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bdir = f"/root/negotiatex-deploy/{stamp}"
    backend = any(not f.startswith("landing/") for f in changed)
    web = [f for f in changed if f.startswith("landing/")]

    print(f"\n1. Sicherung nach {bdir}")
    paths = "\n".join(target(f) for f in changed)
    remote(f"""mkdir -p {bdir}/files && chmod 700 /root/negotiatex-deploy {bdir}
echo "{head}" > {bdir}/commit.txt; cat {REMOTE_APP}/DEPLOYED > {bdir}/previous.txt 2>/dev/null || true
while read -r t; do
  [ -z "$t" ] && continue
  echo "$t" >> {bdir}/paths.txt
  if [[ "$t" == {REMOTE_WEB}/* ]]; then
    if docker exec {NGINX} test -e "$t"; then mkdir -p "{bdir}/files$(dirname "$t")"; docker cp {NGINX}:"$t" "{bdir}/files$t"; fi
  elif [ -e "$t" ]; then mkdir -p "{bdir}/files$(dirname "$t")"; cp -a "$t" "{bdir}/files$t"; fi
done""", stdin=(paths + "\n").encode())

    print("2. Dateien aus dem Commit uebertragen")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in changed:
            info = tarfile.TarInfo(target(f).lstrip("/"))
            info.size, info.mode, info.mtime = len(local[f]), 0o644, int(time.time())
            tar.addfile(info, io.BytesIO(local[f]))
    remote(f"""tar -xzf - -C / --no-same-owner
{''.join(f'docker cp {target(f)} {NGINX}:{target(f)}' + chr(10) for f in web)}echo ok""", stdin=buf.getvalue())

    ok = True
    if backend:
        print("3. Backend neu bauen und starten")
        ok = rebuild_backend()
    print("4. nginx pruefen und neu laden")
    ok = reload_nginx() and ok
    print("5. Pruefungen")
    ok = checks() and ok
    if ok and ui:
        print("6. Browser-Tests nach dem Deploy")
        try:
            ok = ui_run(label="nach-deploy")
        except BaseException as e:  # auch Abbruch (Fehler, Strg+C) fuehrt zum Ruecksprung (QA NX-004)
            print(f"  Browser-Tests abgebrochen: {type(e).__name__} {e}")
            ok = False

    if ok:
        remote(f'echo "{head} {stamp}" > {REMOTE_APP}/DEPLOYED')
        print(f"\nDEPLOY OK -- {head[:7]} ist live. Sicherung: {bdir}")
        return

    print("\nFEHLER -- setze auf die Sicherung zurueck")
    remote(f"""while read -r t; do
  [ -z "$t" ] && continue
  if [ -e "{bdir}/files$t" ]; then
    if [[ "$t" == {REMOTE_WEB}/* ]]; then cp -a "{bdir}/files$t" "$t"; docker cp "{bdir}/files$t" {NGINX}:"$t"
    else cp -a "{bdir}/files$t" "$t"; fi
  else
    rm -f "$t"; [[ "$t" == {REMOTE_WEB}/* ]] && docker exec {NGINX} rm -f "$t"
  fi
done < {bdir}/paths.txt; echo zurueckgesetzt""", check=False)
    if backend:
        rebuild_backend()
    reload_nginx()
    print("Pruefungen nach Ruecksprung:")
    checks()
    raise SystemExit(f"Deploy von {head[:7]} fehlgeschlagen und zurueckgerollt. Sicherung: {bdir}")


if __name__ == "__main__":
    main()
