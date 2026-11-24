// Angebots-Check mit Anlage eines Vorhabens. SCHREIBT Daten (neues Vorhaben, Anfrage-ENTWÜRFE --
// versendet wird nichts, Entwürfe brauchen Freigabe). Läuft deshalb nur mit "--mit-anlage".
// Braucht ein Test-Angebots-PDF: NX_UI_ANGEBOT_PDF.
const { chromium } = require('playwright');
const { B, token, counter, context, watchErrors, shot } = require('./lib');

async function main() {
  const { ok, done } = counter('angebots_check');
  const pdf = process.env.NX_UI_ANGEBOT_PDF;
  ok(!!pdf, 'Test-Angebots-PDF konfiguriert (angebot_pdf)');
  if (!pdf) return done();
  const browser = await chromium.launch();
  try {
    // 1) ohne Anmeldung: Startseite -> CTA -> Login mit Ruecksprung
    let c = await browser.newContext({ viewport: { width: 1366, height: 900 } }); let p = await c.newPage();
    await p.goto(B + '/', { waitUntil: 'networkidle' });
    await p.click('#heroCta1'); await p.waitForTimeout(1500);
    ok(p.url().includes('/login?next=%2Fangebots-check'), 'CTA führt ohne Anmeldung zum Login mit Rücksprung: ' + p.url());
    await c.close();

    // 2) als Testkunde
    c = await context(browser, token('kunde'), false); p = await c.newPage(); const errs = watchErrors(p);
    await p.goto(B + '/angebots-check', { waitUntil: 'networkidle' }); await p.waitForTimeout(800);
    await p.click('#oc-go'); await p.waitForTimeout(600);
    ok(!!(await p.$('#oc-svc.invalid')), 'Fehler am Feld „Art der Leistung“ ohne Eingabe');
    await p.click('[data-cat="Videoproduktion"]');
    await p.fill('#oc-desc', 'Imagevideo ca. 90 Sekunden für eine neue Produktlinie, ein Drehtag in München, inkl. Schnitt und Farbkorrektur, Online-Nutzungsrechte für 2 Jahre.');
    await p.fill('#oc-price', '13400'); await p.fill('#oc-target', '10000'); await p.fill('#oc-reg', 'München');
    await p.setInputFiles('#oc-file', pdf);
    await p.click('#oc-go');
    await p.waitForURL(/\/vorhaben\/[0-9a-f-]{36}$/, { timeout: 120000 }); await p.waitForTimeout(1500);
    const pid = p.url().split('/').pop();
    ok(true, 'Check erstellt, Vorhaben ' + pid);
    const tiles = await p.$$eval('.octiles > div', d => d.map(x => x.innerText.replace(/\s+/g, ' ')));
    ok(tiles[0] && tiles[0].includes('13.400,00'), 'Ihr Angebot 13.400 €');
    ok(tiles[1] && tiles[1].includes('10.000,00') && tiles[1].includes('25,4'), 'Ziel 10.000 € (−25,4 %)');
    ok((await p.$$eval('.finding', d => d.length)) >= 2, 'Vertragsprüfung zeigt Punkte');
    await p.screenshot({ path: shot('angebots_check', 'ergebnis'), fullPage: true });
    await p.click('#oc-start'); await p.waitForTimeout(800);
    ok(!!(await p.$('#oc-ok.invalid')), 'Ohne Bestätigung kein Start (Feldfehler)');
    await p.check('#oc-ok'); await p.click('#oc-start');
    await p.waitForURL(/\/entscheidungen$/, { timeout: 120000 }); await p.waitForTimeout(1500);
    const drafts = await p.$$eval('.draft', d => d.map(x => x.innerText.split('\n').slice(0, 2).join(' ')));
    ok(drafts.length >= 1, `${drafts.length} Anfrage-Entwürfe zur Freigabe`);
    const own = process.env.NX_UI_ANGEBOT_ANBIETER;  // Anbieter des Test-PDFs (angebot_anbieter)
    if (own) ok(!drafts.some(d => d.toLowerCase().includes(own.toLowerCase())), `Anbieter des vorliegenden Angebots (${own}) wird nicht angefragt`);
    else console.log('HINWEIS angebot_anbieter nicht konfiguriert -- Prüfung „Anbieter nicht erneut anfragen“ übersprungen');
    await p.goto(B + `/vorhaben/${pid}/angebote`, { waitUntil: 'networkidle' }); await p.waitForTimeout(800);
    ok((await p.content()).includes('Ihr vorliegendes Angebot'), 'Angebote-Ansicht zeigt Referenzpreis');
    ok(errs.length === 0, 'keine JS-Fehler ' + (errs.length ? JSON.stringify(errs) : ''));
    console.log(`       Hinweis: Testvorhaben ${pid} angelegt (Entwürfe nicht freigeben, bei Bedarf archivieren).`);
    await c.close();
  } finally {
    await browser.close();
  }
  return done();
}

module.exports = main;
if (require.main === module) main().then(f => process.exit(f ? 1 : 0), e => { console.error(e); process.exit(2); });
