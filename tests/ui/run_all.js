// Fuehrt alle Browser-Tests nacheinander aus. Exit 0 nur, wenn alle bestanden.
//   node run_all.js               nur lesende Tests (Standard, auch vor/nach jedem Deploy)
//   node run_all.js --mit-anlage  zusaetzlich Angebots-Check (legt ein Testvorhaben an)
const suites = [['kunde_experte', require('./kunde_experte.test')], ['team', require('./team.test')]];
if (process.argv.includes('--mit-anlage')) suites.push(['angebots_check', require('./angebots_check.test')]);

(async () => {
  const summary = [];
  for (const [name, run] of suites) {
    console.log(`\n===== ${name} =====`);
    try {
      summary.push([name, await run()]);
    } catch (e) {
      console.error(`[${name}] abgebrochen: ${e.message}`);
      summary.push([name, 'Abbruch']);
    }
  }
  console.log('\n===== Ergebnis =====');
  for (const [name, f] of summary) console.log(`${f === 0 ? 'OK    ' : 'FEHLER'} ${name}${f === 0 ? '' : ' (' + f + ')'}`);
  process.exit(summary.every(([, f]) => f === 0) ? 0 : 1);
})();
