// Team-Seite: Admin (Desktop), Experte (Mobil), Kunde gesperrt. Nur lesend.
const { chromium } = require('playwright');
const { B, token, counter, context, watchErrors, shot } = require('./lib');

async function main() {
  const { ok, done } = counter('team');
  const browser = await chromium.launch();
  try {
    for (const [who, mobile] of [['admin', false], ['experte', true], ['kunde', false]]) {
      const c = await context(browser, token(who), mobile);
      const p = await c.newPage(); const errs = watchErrors(p);
      if (who === 'kunde') {
        await p.goto(B + '/arbeitsbereich/team', { waitUntil: 'networkidle' }); await p.waitForTimeout(1500);
        ok(p.url().endsWith('/uebersicht'), 'Kunde: Team-Seite gesperrt → Übersicht');
      } else {
        await p.goto(B + '/arbeitsbereich/team', { waitUntil: 'networkidle' }); await p.waitForTimeout(1200);
        const n = await p.$$eval('.agent', x => x.length); ok(n === 7, `${who}: 7 Agentenkarten (${n})`);
        ok((await p.$$eval('.agent svg.nx-avatar-svg', x => x.length)) === 7, `${who}: 7 Avatare`);
        ok((await p.getAttribute('#app-nav a[aria-current=page]', 'href')) === '/arbeitsbereich', `${who}: Arbeitsübersicht markiert`);
        ok(!!(await p.$('#cost .note')) === (who === 'admin'), `${who}: Kostenhinweis nur für Admin`);
        if (mobile) ok(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Mobil: kein horizontales Scrollen');
        await p.screenshot({ path: shot('team', who + '-team'), fullPage: true });
        await p.click('.agent[data-k="briefing"]'); await p.waitForTimeout(1200);
        ok((await p.$$eval('#dlg .log li', x => x.length)) >= 1, `${who}: Protokoll des Briefing-Assistenten`);
        if (!mobile) {
          await p.goto(B + '/arbeitsbereich', { waitUntil: 'networkidle' }); await p.waitForTimeout(1500);
          ok((await p.$$eval('#team-avatars svg', x => x.length)) === 7, 'Arbeitsübersicht: Team-Leiste mit 7 Avataren');
        }
      }
      ok(errs.length === 0, `${who}: keine JS-Fehler ${errs.length ? JSON.stringify(errs) : ''}`);
      await c.close();
    }
  } finally {
    await browser.close();
  }
  return done();
}

module.exports = main;
if (require.main === module) main().then(f => process.exit(f ? 1 : 0), e => { console.error(e); process.exit(2); });
