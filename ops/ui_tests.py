#!/usr/bin/env python3
"""
Browser-Tests (Playwright) gegen negotiatex.ai -- ein Befehl fuer alle.

    python ops/ui_tests.py                nur lesende Tests (Kunde, Experte, Team)
    python ops/ui_tests.py --mit-anlage   zusaetzlich Angebots-Check (legt ein Testvorhaben mit Entwuerfen an)

Ablauf:
1. Kurzlebige Anmelde-Tokens (20 Minuten) fuer die Testkonten werden auf dem Server im Backend-Container
   erzeugt (Schluessel bleibt auf dem Server) und nur in einem temporaeren Verzeichnis abgelegt.
2. Die Tests laufen lokal mit Node und Playwright (tests/ui, Abhaengigkeiten werden bei Bedarf installiert).
3. Ergebnis mit Datum in ops/ui-results/<Zeit>.log (nicht im Repository), Screenshots daneben.
4. Die Tokens werden danach geloescht.

Die Adressen der Testkonten stehen NICHT im Repository, sondern in ops/ui_tests.local.json:
    {"kunde": "...", "experte": "...", "admin": "...",
     "angebot_pdf": "C:/pfad/zum/test-angebot.pdf", "angebot_anbieter": "Name des Anbieters im PDF"}
"angebot_pdf" und "angebot_anbieter" werden nur fuer --mit-anlage gebraucht.

Exit-Code 0 = alle Tests bestanden. ops/deploy.py ruft dieses Skript vor und nach jedem Deploy auf.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(REPO, "tests", "ui")
CONFIG = os.path.join(REPO, "ops", "ui_tests.local.json")
RESULTS = os.path.join(REPO, "ops", "ui-results")
ROLES = ("kunde", "experte", "admin")
TOKEN_MINUTES = 20

# Laeuft im Backend-Container: Konten per E-Mail nachschlagen und kurzlebige Tokens signieren.
# Gibt nur die Tokens aus (JSON), nie den Schluessel.
MINT = r"""
import asyncio, json, sys, time
sys.path.insert(0, "/app")
import jwt
from sqlalchemy import text
from database import admin_engine
from routers.auth import SECRET

async def main(roles):
    out = {}
    async with admin_engine.connect() as c:
        for role, email in roles.items():
            row = (await c.execute(text("select id, email, is_admin from users where lower(email) = lower(:e)"), {"e": email})).first()
            if not row:
                out[role] = None
                continue
            now = int(time.time())
            out[role] = jwt.encode({"id": str(row.id), "email": row.email, "is_admin": bool(row.is_admin),
                                    "iat": now, "exp": now + %d * 60, "purpose": "ui-test"}, SECRET, algorithm="HS256")
    print(json.dumps(out))

asyncio.run(main(json.loads(sys.argv[1])))
""" % TOKEN_MINUTES


def ssh_cmd():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from deploy import SSH  # gleicher Zugang wie der Deploy (Schluessel, kein Passwort)
    return SSH


def load_config():
    if not os.path.exists(CONFIG):
        raise SystemExit(f"{CONFIG} fehlt. Inhalt: {{\"kunde\": \"...\", \"experte\": \"...\", \"admin\": \"...\"}}")
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [r for r in ROLES if not cfg.get(r)]
    if missing:
        raise SystemExit(f"In {CONFIG} fehlen: {', '.join(missing)}")
    return cfg


def mint_tokens(cfg) -> dict:
    roles = {r: cfg[r] for r in ROLES}
    remote = "docker exec -i negotiatex-backend python - " + shlex.quote(json.dumps(roles))
    r = subprocess.run(ssh_cmd() + ["bash -c " + shlex.quote(remote)], input=MINT.encode(), capture_output=True)
    if r.returncode != 0:
        raise SystemExit("Tokens konnten nicht erzeugt werden:\n" + r.stderr.decode(errors="replace")[-2000:])
    tokens = json.loads(r.stdout.decode().strip().splitlines()[-1])
    unknown = [r for r, t in tokens.items() if not t]
    if unknown:
        raise SystemExit(f"Testkonto nicht gefunden: {', '.join(unknown)} (Adressen in {CONFIG} pruefen)")
    return tokens


def ensure_playwright():
    npm = shutil.which("npm")
    if not npm or not shutil.which("node"):
        raise SystemExit("Node.js/npm nicht gefunden -- bitte Node.js installieren.")
    if not os.path.isdir(os.path.join(UI_DIR, "node_modules", "playwright")):
        print("Installiere Playwright fuer tests/ui ...")
        subprocess.run([npm, "ci" if os.path.exists(os.path.join(UI_DIR, "package-lock.json")) else "install",
                        "--no-audit", "--no-fund"], cwd=UI_DIR, check=True)
        npx = shutil.which("npx")
        if not npx:
            raise SystemExit("npx nicht gefunden -- bitte Node.js vollstaendig installieren.")
        subprocess.run([npx, "playwright", "install", "chromium"], cwd=UI_DIR, check=True)


def run(write: bool = False, label: str = "") -> bool:
    """Fuehrt die Tests aus, schreibt das Protokoll und gibt True bei Erfolg zurueck."""
    cfg = load_config()
    ensure_playwright()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    os.makedirs(RESULTS, exist_ok=True)
    log_path = os.path.join(RESULTS, f"{stamp}{'_' + label if label else ''}.log")
    tdir = tempfile.mkdtemp(prefix="nx-ui-")
    try:
        for role, tk in mint_tokens(cfg).items():
            p = os.path.join(tdir, role)
            with open(p, "w", encoding="utf-8") as f:
                f.write(tk)
            os.chmod(p, 0o600)
        env = dict(os.environ, NX_UI_TOKENS=tdir, NX_UI_OUT=os.path.join(RESULTS, stamp))
        if cfg.get("angebot_pdf"):
            env["NX_UI_ANGEBOT_PDF"] = cfg["angebot_pdf"]
        if cfg.get("angebot_anbieter"):
            env["NX_UI_ANGEBOT_ANBIETER"] = cfg["angebot_anbieter"]
        if cfg.get("base"):
            env["NX_UI_BASE"] = cfg["base"]
        args = [shutil.which("node"), "run_all.js"] + (["--mit-anlage"] if write else [])
        start = datetime.now()
        code = None
        with open(log_path, "w", encoding="utf-8") as log:
            log.write(f"Browser-Tests {start:%d.%m.%Y %H:%M:%S}{' (' + label + ')' if label else ''}\n")
            proc = subprocess.Popen(args, cwd=UI_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                for raw in proc.stdout:
                    log.write(raw.decode("utf-8", errors="replace"))
                    # Bytes direkt ausgeben: ohne Konsolenfenster (Umleitung, cp1252) wuerden Zeichen wie
                    # "→" sonst einen UnicodeEncodeError ausloesen (QA NX-004).
                    out = getattr(sys.stdout, "buffer", None)
                    if out is not None:
                        out.write(b"  " + raw)
                        out.flush()
                    else:
                        sys.stdout.write("  " + raw.decode("utf-8", errors="replace"))
                code = proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                end = datetime.now()
                log.write(f"\nEnde {end:%d.%m.%Y %H:%M:%S}, Dauer {int((end - start).total_seconds())} s, "
                          f"Exit-Code {code if code is not None else 'Abbruch'}\n")
    finally:
        shutil.rmtree(tdir, ignore_errors=True)
    print(f"Protokoll: {log_path}")
    return code == 0


if __name__ == "__main__":
    sys.exit(0 if run(write="--mit-anlage" in sys.argv) else 1)
