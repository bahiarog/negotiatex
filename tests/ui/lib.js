// Gemeinsame Helfer der Browser-Tests. Konfiguration kommt aus Umgebungsvariablen, die
// ops/ui_tests.py setzt -- im Repository stehen keine Zugangsdaten und keine Kontoadressen.
//   NX_UI_BASE    Basis-URL (Standard https://negotiatex.ai)
//   NX_UI_TOKENS  Verzeichnis mit kurzlebigen Tokens: kunde, experte, admin (je eine Datei)
//   NX_UI_OUT     Verzeichnis fuer Screenshots
const fs = require('fs');
const path = require('path');

const B = process.env.NX_UI_BASE || 'https://negotiatex.ai';
const OUT = process.env.NX_UI_OUT || path.join(__dirname, 'out');

function token(role) {
  const dir = process.env.NX_UI_TOKENS;
  if (!dir) throw new Error('NX_UI_TOKENS fehlt -- Tests ueber "python ops/ui_tests.py" starten.');
  return fs.readFileSync(path.join(dir, role), 'utf8').trim();
}

function counter(name) {
  let fails = 0;
  const ok = (c, msg) => { console.log((c ? 'OK     ' : 'FEHLER ') + msg); if (!c) fails++; };
  const done = () => { console.log(fails ? `\n[${name}] ${fails} FEHLER` : `\n[${name}] ALLE PRÜFUNGEN BESTANDEN`); return fails; };
  return { ok, done };
}

async function context(browser, tk, mobile) {
  const c = await browser.newContext(mobile
    ? { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true }
    : { viewport: { width: 1366, height: 900 } });
  await c.addInitScript(t => { try { localStorage.setItem('nx_token', t); localStorage.setItem('nx_user', '{}'); sessionStorage.clear(); } catch (e) {} }, tk);
  return c;
}

// Sammelt JS-Fehler einer Seite (ohne Favicon/Ressourcen-Rauschen).
function watchErrors(page) {
  const errs = [];
  page.on('pageerror', e => errs.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !/favicon|Failed to load resource/.test(m.text())) errs.push('console: ' + m.text()); });
  return errs;
}

async function api(tk, p) {
  const r = await fetch(B + p, { headers: { Authorization: 'Bearer ' + tk } });
  if (!r.ok) throw new Error(`${p} -> ${r.status}`);
  return r.json();
}

function shot(sub, name) {
  const dir = path.join(OUT, sub);
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, name + '.png');
}

module.exports = { B, OUT, token, counter, context, watchErrors, api, shot };
