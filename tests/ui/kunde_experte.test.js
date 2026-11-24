// UI-Abnahme Kunde (Desktop, Mobil) und Experte. Nur lesend: es wird nichts gespeichert oder versendet.
const { chromium } = require('playwright');
const { B, token, counter, context, watchErrors, api, shot } = require('./lib');

async function main() {
  const { ok, done } = counter('kunde_experte');
  const TK = token('kunde'), TE = token('experte');
  // Vorhaben des Testkunden selbst ermitteln (frueher feste PID): bevorzugt eines, das auf den Kunden wartet.
  const list = await api(TK, '/api/v1/projects');
  const pick = list.find(p => p.state === 'waiting_for_you') || list[0];
  ok(!!pick, `Testkunde hat mindestens ein Vorhaben (${list.length})`);
  if (!pick) return done();
  const PID = pick.id;
  const waiting = pick.state === 'waiting_for_you';

  const browser = await chromium.launch();
  try {
    let c = await context(browser, TK, false); let p = await c.newPage();
    let errs = watchErrors(p);
    async function visit(pathname, name) {
      errs.length = 0;
      await p.goto(B + pathname, { waitUntil: 'networkidle' });
      await p.waitForTimeout(700);
      if (name) await p.screenshot({ path: shot('kunde_experte', name), fullPage: true });
      ok(errs.length === 0, `${pathname} ohne JS-Fehler ${errs.length ? JSON.stringify(errs) : ''}`);
    }

    // ---------- Kunde, Desktop ----------
    await visit('/uebersicht', 'k-uebersicht');
    const nav = await p.$$eval('#app-nav .nx-link', as => as.map(a => a.textContent.trim()));
    ok(JSON.stringify(nav.map(x => x.replace(/\d+$/, ''))) === JSON.stringify(['Übersicht', 'Meine Vorhaben', 'Einsparungen', 'Entscheidungen']), 'Kundennavigation: ' + nav.join(' | '));
    ok(!(await p.$('#app-nav .nx-switch')), 'Kein Bereichswechsel für reinen Kunden');
    ok(!!(await p.$('#app-nav .nx-new')) && !!(await p.$('#app-nav .nx-help')) && !!(await p.$('#nx-profile')), 'Neues Vorhaben, Hilfe und Profil in der Kopfleiste');
    ok((await p.getAttribute('#app-nav a[aria-current=page]', 'href')) === '/uebersicht', 'Aktueller Menüpunkt markiert');
    ok((await p.getAttribute('#app-nav .nx-logo', 'href')) === '/uebersicht', 'Logo führt zur Übersicht');

    const listPath = waiting ? '/vorhaben?status=waiting_for_you' : '/vorhaben';
    await visit(listPath, 'k-liste');
    await p.click(`a[href="/vorhaben/${PID}"]`); await p.waitForTimeout(1200);
    ok(p.url().endsWith('/vorhaben/' + PID), 'Detail aus Liste geöffnet');
    const crumb = await p.$eval('.crumbs a', a => a.getAttribute('href'));
    ok(crumb === listPath, 'Breadcrumb führt zur Liste zurück: ' + crumb);
    await p.click('.crumbs a'); await p.waitForTimeout(1200);
    ok(p.url().endsWith(listPath), 'Rückweg erhält Filter: ' + p.url());
    if (waiting) ok((await p.getAttribute('[data-st="waiting_for_you"]', 'aria-pressed')) === 'true', 'Filter-Schaltfläche weiterhin aktiv');
    else console.log('HINWEIS Filterprüfung übersprungen: kein Vorhaben des Testkunden wartet auf ihn');
    await p.goBack(); await p.waitForTimeout(1000);
    ok(p.url().endsWith('/vorhaben/' + PID), 'Browser-Zurück funktioniert ebenfalls');

    for (const t of ['angebote', 'entscheidungen', 'dokumente']) await visit(`/vorhaben/${PID}/${t}`, 'k-' + t);
    ok(!(await p.$('.tabs a[href$="/bearbeitung"]')), 'Kein Bearbeitungs-Tab für Kunden');
    await visit('/entscheidungen', 'k-entscheidungen-global');
    await visit('/einsparungen', 'k-einsparungen');
    ok((await p.content()).includes('noch nicht ermittelt'), 'Fehlende Ergebnisse als „noch nicht ermittelt“');
    await visit('/hilfe', 'k-hilfe');

    // Wizard: Eingabe, Reload -> wiederhergestellt; Feldfehler. Es wird nichts abgeschickt.
    await visit('/vorhaben/neu', 'k-neu-1');
    await p.fill('#w-in', 'Abnahmetest Eingabeschutz: Übersetzung Website DE-EN, 30 Seiten.');
    await p.reload({ waitUntil: 'networkidle' }); await p.waitForTimeout(600);
    ok((await p.inputValue('#w-in')).includes('Eingabeschutz'), 'Ungespeicherte Eingabe nach Neuladen wiederhergestellt');
    await p.click('#w-next'); await p.waitForTimeout(800);
    ok(!!(await p.$('#w-ceil.invalid')) && (await p.textContent('#err-budget_ceiling')).includes('Budget'), 'Fehler am Feld „Budget-Obergrenze“ erklärt');
    await p.evaluate(() => localStorage.removeItem('nx_wiz:neu'));

    // Interne URLs direkt aufrufen -> gesperrt
    await p.goto(B + '/data-center', { waitUntil: 'networkidle' }); await p.waitForTimeout(1500);
    ok(p.url().endsWith('/uebersicht'), 'Kunde: /data-center leitet zur Übersicht um (' + p.url() + ')');
    await p.goto(B + '/arbeitsbereich/vorhaben', { waitUntil: 'networkidle' }); await p.waitForTimeout(1500);
    ok(!p.url().includes('/arbeitsbereich'), 'Kunde: /arbeitsbereich/vorhaben gesperrt (' + p.url() + ')');
    await c.close();

    // ---------- Kunde, Mobil ----------
    c = await context(browser, TK, true); p = await c.newPage(); errs = watchErrors(p);
    const noOverflow = () => p.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
    await visit('/uebersicht', 'm-uebersicht');
    ok(await noOverflow(), 'Mobil: kein horizontales Scrollen');
    await p.click('#nx-menu'); await p.waitForTimeout(300);
    ok(await p.isVisible('#nx-links'), 'Mobil: Menü öffnet sich');
    for (const [pth, name] of [[`/vorhaben/${PID}`, 'Vorhaben-Detail'], [`/vorhaben/${PID}/entscheidungen`, 'Entscheidungen'],
                               ['/einsparungen', 'Einsparungen'], ['/vorhaben', 'Liste']]) {
      await visit(pth, 'm-' + name.toLowerCase());
      ok(await noOverflow(), `Mobil: ${name} ohne horizontales Scrollen`);
    }
    await p.keyboard.press('Tab');
    ok(await p.evaluate(() => !!document.activeElement && document.activeElement !== document.body), 'Tastatur: Fokus erreicht Bedienelemente');
    await c.close();

    // ---------- Experte ----------
    c = await context(browser, TE, false); p = await c.newPage(); errs = watchErrors(p);
    await visit('/arbeitsbereich', 'e-arbeitsuebersicht');
    const wnav = await p.$$eval('#app-nav .nx-link', as => as.map(a => a.textContent.trim().replace(/\d+$/, '')));
    ok(JSON.stringify(wnav) === JSON.stringify(['Arbeitsübersicht', 'Vorhaben & Aufgaben', 'Lieferanten & Ausschreibungen', 'Bestellungen & Verträge', 'Data Center']), 'Arbeitsbereich-Navigation (ohne Administration): ' + wnav.join(' | '));
    ok(!!(await p.$('#app-nav .nx-switch')), 'Bereichswechsel für Experten sichtbar');
    await visit('/arbeitsbereich/vorhaben', 'e-vorhaben');
    await visit(`/arbeitsbereich/vorhaben/${PID}/bearbeitung`, 'e-bearbeitung');
    ok(!!(await p.$('a[href="/vorhaben/' + PID + '"]')), '„Kundenansicht ansehen“ vorhanden');
    await visit('/sourcing-dashboard', 'e-sourcing');
    ok((await p.getAttribute('#app-nav a[aria-current=page]', 'href')) === '/sourcing-dashboard', 'Lieferanten & Ausschreibungen markiert');
    await p.goto(B + '/administration', { waitUntil: 'networkidle' }); await p.waitForTimeout(1500);
    ok(p.url().endsWith('/uebersicht'), 'Experte: /administration gesperrt (' + p.url() + ')');
    await c.close();
  } finally {
    await browser.close();
  }
  return done();
}

module.exports = main;
if (require.main === module) main().then(f => process.exit(f ? 1 : 0), e => { console.error(e); process.exit(2); });
