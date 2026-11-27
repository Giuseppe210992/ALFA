// Test end-to-end della webapp come la userebbe una persona (richiede Playwright per Node).
//
//   node tests/e2e_webapp.js <cartella_progetto> <cartella_screenshot>
//
// 1. avvia un finto provider OpenAI e `python run_webapp.py` in una copia pulita del progetto
// 2. apre il link stampato nel terminale (login automatico)
// 3. collega un account dal pannello «Account e chiavi», preme Salva (→ Prova automatica)
// 4. fa una domanda, poi un batch, scarica il CSV, e controlla i risultati
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const proj = path.resolve(process.argv[2] || path.join(__dirname, '..'));
const shots = path.resolve(process.argv[3] || path.join(proj, 'e2e_screens'));
fs.mkdirSync(shots, { recursive: true });

function startProc(cmd, args, opts, readyRe) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, { ...opts, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    const t = setTimeout(() => reject(new Error('timeout avvio ' + args.join(' ') + '\n' + out)), 20000);
    const on = d => {
      out += d.toString();
      const m = out.match(readyRe);
      if (m) { clearTimeout(t); resolve({ p, m, out: () => out }); }
    };
    p.stdout.on('data', on); p.stderr.on('data', on);
    p.on('exit', c => { clearTimeout(t); reject(new Error(`processo uscito (${c}):\n${out}`)); });
  });
}

const assert = (c, m) => { if (!c) throw new Error('ASSERT: ' + m); console.log('  ✓ ' + m); };

(async () => {
  const env = { ...process.env, PYTHONUNBUFFERED: '1' };
  delete env.HTTP_PROXY; delete env.http_proxy;
  const fake = await startProc('python3', ['-m', 'tests.fake_openai', '0'], { cwd: proj, env }, /http:\/\/127\.0\.0\.1:(\d+)\/v1/);
  // la porta 0 non è utile qui: rilancio su una porta nota
  fake.p.kill();
  const fport = 18000 + Math.floor(Math.random() * 1000);
  const planFile = path.join(proj, 'e2e_plan.json');
  fs.writeFileSync(planFile, JSON.stringify({
    title: 'Demo App', summary: 'Ripete un testo e lo salva in un file.', gpu: false, setup: ['echo installazione-ok'],
    run: 'python app.py --text {testo} --repeat {ripetizioni} --input {file} --out {OUT}',
    inputs: [{ key: 'testo', label: 'Testo da ripetere', type: 'text', required: true },
             { key: 'ripetizioni', label: 'Quante volte', type: 'number', default: '2', required: false },
             { key: 'file', label: 'File di dati', type: 'url', required: true }],
    outputs: [], timeout_min: 5 }));
  const fake2 = await startProc('python3', ['-m', 'tests.fake_openai', String(fport)], { cwd: proj, env: { ...env, FAKE_PLAN_JSON: planFile } }, /fake OpenAI su (\S+)/);
  const fakeUrl = fake2.m[1];

  const kport = 19000 + Math.floor(Math.random() * 500), gport = 19600 + Math.floor(Math.random() * 300);
  const ghDir = path.join(proj, 'e2e_gh');
  const fkag = await startProc('python3', ['-m', 'tests.fake_kaggle', String(kport)], { cwd: proj, env }, /fake Kaggle su (\S+)/);
  const fgh = await startProc('python3', ['-m', 'tests.fake_github', String(gport), ghDir], { cwd: proj, env }, /fake GitHub (\{.*\})/);
  Object.assign(env, JSON.parse(fgh.m[1]), { GPOOL_KAGGLE_API: fkag.m[1] });
  const app = await startProc('python3', ['run_webapp.py', '--no-browser'], { cwd: proj, env }, /Apri:\s+(http:\/\/\S+)/);
  const loginUrl = app.m[1];
  const baseUrl = loginUrl.replace(/\/#t=.*/, '');
  console.log('webapp:', loginUrl.replace(/#t=.*/, '#t=***'));

  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  // i 400 sono risposte volute (token/link sbagliati inseriti dal test): la webapp li mostra come messaggio
  page.on('console', m => { if (m.type() === 'error' && !/status of 400/.test(m.text())) errors.push(m.text()); });
  try {
    // 1. login automatico dal link del terminale
    await page.goto(loginUrl);
    await page.waitForSelector('#view-app:not(.hide)', { timeout: 10000 });
    assert(!page.url().includes('#t='), 'il token viene tolto dalla barra degli indirizzi');
    await page.waitForSelector('#conn-txt:has-text("Connesso")');
    // 2. senza account: il pannello si apre da solo
    await page.waitForSelector('#accounts-card:not(.hide)', { timeout: 10000 });
    assert(await page.locator('.prov').count() >= 6, 'pannello account con i provider gratuiti');
    await page.screenshot({ path: path.join(shots, '1_primo_avvio_account.png'), fullPage: true });

    // 3. chiave sbagliata → errore chiaro
    const custom = page.locator('.prov[data-name="custom"]');
    await custom.locator('[data-f=base_url]').first().fill(fakeUrl);
    await custom.locator('[data-k=key]').fill('bad');
    await custom.locator('[data-a=save]').click();
    await page.waitForSelector('.prov[data-name="custom"] .out.bad', { timeout: 15000 });
    const badTxt = await page.locator('.prov[data-name="custom"] .out').textContent();
    assert(/401/.test(badTxt), 'chiave non valida segnalata: ' + badTxt.trim().slice(0, 80));

    // 4. chiave giusta → Prova automatica OK
    await page.locator('.prov[data-name="custom"] [data-k=key]').fill('good');
    await page.locator('.prov[data-name="custom"] [data-a=save]').click();
    await page.waitForSelector('.prov[data-name="custom"] .out.ok', { timeout: 15000 });
    const okTxt = await page.locator('.prov[data-name="custom"] .out').textContent();
    assert(/Funziona/.test(okTxt), 'pulsante Prova: ' + okTxt.trim().slice(0, 60));
    await page.screenshot({ path: path.join(shots, '2_account_collegato.png'), fullPage: true });
    const secrets = fs.readFileSync(path.join(proj, 'config', 'secrets.env'), 'utf8');
    assert(/CUSTOM_API_KEY=good/.test(secrets), 'chiave salvata in config/secrets.env');
    await page.click('#btn-accounts-close');
    await page.waitForTimeout(300);
    assert(await page.locator('#onboard').isHidden(), 'avviso «collega un account» sparito');

    // 5. domanda
    await page.click('[data-mode=chat]');
    await page.click('#btn-next1');
    await page.fill('#chat-text', 'Spiega un MoE in una frase');
    await page.click('#btn-next2');
    await page.click('#btn-run');
    await page.waitForSelector('#results .res .body', { timeout: 20000 });
    const ans = await page.locator('#results .res .body').first().textContent();
    assert(ans.includes('OK: Spiega un MoE in una frase'), 'risposta mostrata: ' + ans.slice(0, 50));
    await page.waitForSelector('#jobs .tag.ok');
    await page.screenshot({ path: path.join(shots, '3_risposta.png'), fullPage: true });

    // 6. batch
    await page.click('[data-mode=batch]');
    await page.click('#btn-next1');
    await page.fill('#batch-instr', 'Sentiment in una parola');
    await page.fill('#batch-lines', 'Ottimo prodotto\nPessimo servizio\nNella media\nLo ricomprerei\nMai più');
    await page.click('#btn-next2');
    assert((await page.locator('#summary').textContent()).includes('5 elementi'), 'riepilogo batch: 5 elementi');
    await page.click('#btn-run');
    await page.waitForFunction(() => document.querySelectorAll('#results .res').length === 5, null, { timeout: 30000 });
    const texts = await page.locator('#results .res .body').allTextContents();
    assert(texts[1].includes('Pessimo servizio'), 'batch: 5 risultati in ordine');
    assert((await page.locator('#results .res').first().textContent()).includes('Ottimo prodotto'), 'testo originale accanto alla risposta');
    const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#btn-dl-csv')]);
    const csv = fs.readFileSync(await dl.path(), 'utf8');
    assert(csv.split('\r\n').length === 6 && csv.includes('Mai più'), 'download CSV corretto');
    await page.screenshot({ path: path.join(shots, '4_batch.png'), fullPage: true });

    // 6b. ricostruzione 3D: modo dedicato, avviso senza GPU, job in coda, poi nodo GPU -> file scaricabili
    await page.click('[data-mode="3d"]');
    await page.click('#btn-next1');
    await page.fill('#d3-url', 'non-un-link');
    await page.click('#btn-next2');
    assert(await page.locator('#wiz-2').isVisible(), '3D: link non valido bloccato nel passo 2');
    await page.fill('#d3-url', 'https://esempio.org/stanza.mp4');
    await page.click('#btn-next2');
    assert((await page.locator('#summary').textContent()).includes('Non hai ancora collegato Kaggle'), '3D: avviso «Kaggle non collegato» nel riepilogo');
    await page.screenshot({ path: path.join(shots, '6_3d_riepilogo.png'), fullPage: true });
    await page.click('#btn-back3');            // si torna al passo 2 (dove c'è il modulo)
    // trascinare uno ZIP dal computer nel modo 3D
    const zipPath = path.join(proj, 'e2e_foto.zip');
    require('child_process').execFileSync('python3', ['-c', `import zipfile; z=zipfile.ZipFile(${JSON.stringify(zipPath)},'w'); [z.writestr('s/%d.jpg'%i,b'x') for i in range(3)]; z.close()`]);
    await page.fill('#d3-url', '');
    await page.locator('#d3-drop input[type=file]').setInputFiles(zipPath);
    await page.waitForSelector('#d3-drop .done:not(.hide)', { timeout: 15000 });
    assert((await page.locator('#d3-drop .done').textContent()).includes('e2e_foto.zip'), '3D: file ZIP caricato dal computer (zona «trascina qui»)');
    assert(await page.locator('#d3-kind').inputValue() === 'images', '3D: uno ZIP imposta da solo «ZIP di foto»');
    await page.click('#btn-next2');
    assert((await page.locator('#summary').textContent()).includes('e2e_foto.zip'), '3D: il riepilogo mostra il file caricato (senza link)');
    await page.click('#btn-back3');
    await page.click('#d3-drop .x');
    assert(await page.locator('#d3-drop .done').isHidden(), '3D: il file si può rimuovere');
    await page.click('#btn-back2');
    // lavoro 3D "vero" via modalità esperta, con il finto repository di test
    const photos = path.join(proj, 'e2e_photos');
    fs.mkdirSync(photos, { recursive: true });
    for (let i = 0; i < 4; i++) fs.writeFileSync(path.join(photos, `00${i}.png`), 'fake' + i);
    await page.click('[data-mode=advanced]');
    await page.click('#btn-next1');
    await page.selectOption('#adv-adapter', 'lingbot_map');
    await page.fill('#adv-params', JSON.stringify({ title: '3D e2e', options: {
      repo_dir: path.join(proj, 'tests', 'stub_lingbot'), model_path: path.join(proj, 'config', 'pool_config.yaml'),
      require_gpu: false, check_deps: false } }));
    await page.fill('#adv-items', JSON.stringify([{ id: 'stanza', images: photos }]));
    await page.click('#btn-next2');
    await page.click('#btn-run');
    await page.waitForSelector('#jobs .job:first-child .tag:has-text("in coda")', { timeout: 15000 });
    assert(true, '3D: senza nodo GPU il lavoro resta «in coda»');
    const gpu = spawn('python3', ['-c', `
import sys; sys.path.insert(0,'.')
from core.config import load_config; from core.state_store import open_store; from worker.worker_daemon import Worker
cfg = load_config('config/pool_config.yaml')
Worker('${baseUrl}', cfg['token'], open_store(cfg), name='gpu-e2e', poll=0.3, hb=1, verbose=False).run()
`], { cwd: proj, env, stdio: 'ignore' });
    try {
      await page.waitForSelector('#jobs .job:first-child .tag.ok', { timeout: 90000 });
      await page.click('#jobs .job:first-child');
      await page.waitForSelector('#filebtns button', { timeout: 15000 });
      const labels = await page.locator('#filebtns button').allTextContents();
      assert(labels.some(l => l.includes('camera_poses.json')) && labels.some(l => l.includes('predictions.npz')), '3D: pulsanti di download: ' + labels.map(l => l.replace('⬇ ', '').split(' ')[0]).join(', '));
      const [dl3] = await Promise.all([page.waitForEvent('download'), page.locator('#filebtns button', { hasText: 'camera_poses.json' }).click()]);
      const poses = JSON.parse(fs.readFileSync(await dl3.path(), 'utf8'));
      assert(poses.frames.length === 4 && dl3.suggestedFilename() === 'camera_poses.json', '3D: file scaricato dal browser (4 pose)');
      await page.screenshot({ path: path.join(shots, '7_3d_risultati.png'), fullPage: true });
    } finally { gpu.kill(); }

    // 6c. account GPU (Kaggle) dal pannello, poi «Programma da GitHub» senza scrivere una riga di codice
    await page.click('#btn-accounts');
    await page.waitForSelector('#kaggle-box');
    assert(await page.locator('#kg-steps').isVisible(), 'GPU: istruzioni passo-passo visibili finché Kaggle non è collegato');
    assert((await page.locator('#kaggle-box').textContent()).includes('kaggle.com/settings/api'), 'GPU: link alla pagina dei token');
    assert((await page.locator('.prov', { hasText: 'Google Colab' }).textContent()).includes('non collegabile'), 'GPU: Colab spiegato onestamente');
    await page.fill('#kg-token', 'token-sbagliato');
    await page.click('#kg-save');
    await page.waitForSelector('#kg-out.bad');
    assert((await page.locator('#kg-out').textContent()).includes('non valido'), 'GPU: token sbagliato → messaggio chiaro');
    await page.fill('#kg-token', 'good-token');
    await page.click('#kg-save');
    await page.waitForSelector('#kg-out.ok');
    assert((await page.locator('#kg-out').textContent()).includes('tester'), 'GPU: collegato come «tester»');
    assert((await page.locator('#kg-quota').textContent()).includes('28'), 'GPU: ore GPU rimaste mostrate (28 h)');
    assert(await page.locator('#kg-steps').isHidden(), 'GPU: istruzioni nascoste dopo il collegamento');
    await page.screenshot({ path: path.join(shots, '8_gpu_kaggle.png'), fullPage: true });
    // l'IA risponde con il piano (chiave speciale del finto provider)
    await page.locator('.prov[data-name="custom"] [data-k=key]').fill('plan');
    await page.locator('.prov[data-name="custom"] [data-a=save]').click();
    await page.waitForSelector('.prov[data-name="custom"] .out.ok');
    await page.click('#btn-accounts-close');

    await page.click('[data-mode=repo]');
    await page.click('#btn-next1');
    await page.fill('#repo-url', 'non-un-link');
    await page.click('#btn-analyze');
    await page.waitForSelector('#repo-status.err');
    assert(true, 'GitHub: link non valido → errore comprensibile: ' + (await page.locator('#repo-status').textContent()).slice(0, 60));
    await page.fill('#repo-url', 'https://github.com/o/demo');
    await page.click('#btn-analyze');
    await page.waitForSelector('#repo-plan:not(.hide)', { timeout: 30000 });
    assert((await page.locator('#rp-src').textContent()).includes('IA'), 'GitHub: analisi con IA');
    assert((await page.locator('#rp-summary').textContent()).includes('Ripete un testo'), 'GitHub: spiegazione in italiano');
    assert(await page.locator('#rpi-testo').isVisible() && await page.locator('#rpi-ripetizioni').isVisible(), 'GitHub: il modulo dei campi si genera da solo');
    await page.screenshot({ path: path.join(shots, '9_repo_analisi.png'), fullPage: true });
    await page.click('#btn-next2');
    assert(await page.locator('#wiz-2').isVisible(), 'GitHub: campo obbligatorio vuoto blocca il passo successivo');
    await page.fill('#rpi-testo', 'ciao dal browser');
    await page.fill('#rpi-ripetizioni', '3');
    await page.click('#btn-next2');
    assert(await page.locator('#wiz-2').isVisible(), 'GitHub: il file obbligatorio mancante blocca il passo successivo');
    const dataFile = path.join(proj, 'e2e_dati.txt');
    fs.writeFileSync(dataFile, 'uno\ndue\ntre\n');
    await page.locator('#drop-rpi-file input[type=file]').setInputFiles(dataFile);
    await page.waitForSelector('#drop-rpi-file .done:not(.hide)', { timeout: 15000 });
    assert((await page.locator('#drop-rpi-file .done').textContent()).includes('e2e_dati.txt'), 'GitHub: file caricato dal computer nel campo «File di dati»');
    await page.click('#btn-next2');
    const sum = await page.locator('#summary').textContent();
    assert(sum.includes('o/demo') && sum.includes('ciao dal browser') && sum.includes('e2e_dati.txt') && sum.includes('Kaggle'), 'GitHub: riepilogo con progetto, valori e Kaggle');
    await page.click('#btn-run');
    await page.waitForSelector('#jobs .job:first-child .tag.ok', { timeout: 90000 });
    await page.waitForFunction(() => document.querySelector('#results').textContent.includes('Completato in'), null, { timeout: 30000 });
    assert(true, 'GitHub: risultato «Completato» mostrato nel pannello Risultati');
    const [dl4] = await Promise.all([page.waitForEvent('download'), page.locator('#filebtns button', { hasText: 'result.txt' }).click()]);
    assert(fs.readFileSync(await dl4.path(), 'utf8') === 'ciao dal browser\nciao dal browser\nciao dal browser\nrighe nel file di input: 3\n', 'GitHub: il file trascinato dal PC è arrivato al programma (3 righe) e il risultato si scarica');
    await page.screenshot({ path: path.join(shots, '10_repo_risultato.png'), fullPage: true });

    // 7. ricarica pagina: resta autenticato
    await page.reload();
    await page.waitForSelector('#conn-txt:has-text("Connesso")');
    assert(await page.locator('#jobs .job').count() >= 2, 'dopo il ricaricamento i lavori sono ancora lì');

    // 8. vista mobile
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(300);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    assert(!overflow, 'nessuno scroll orizzontale su telefono');
    await page.screenshot({ path: path.join(shots, '5_mobile.png'), fullPage: true });

    assert(errors.length === 0, 'nessun errore JavaScript in console' + (errors.length ? ': ' + errors.join(' | ') : ''));

    // 9. token sbagliato → schermata di login
    await page.evaluate(() => localStorage.setItem('pooltok', 'sbagliato'));
    await page.reload();
    await page.waitForSelector('#view-login:not(.hide)');
    assert(true, 'token sbagliato → richiesta login');
    console.log('\nE2E OK');
  } catch (e) {
    await page.screenshot({ path: path.join(shots, 'ERRORE.png'), fullPage: true }).catch(() => {});
    console.error('\nE2E FALLITO:', e.message, '\n--- log webapp ---\n' + app.out());
    process.exitCode = 1;
  } finally {
    await browser.close();
    app.p.kill(); fake2.p.kill(); fkag.p.kill(); fgh.p.kill();
  }
})();
