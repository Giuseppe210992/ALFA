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
  const fake2 = await startProc('python3', ['-m', 'tests.fake_openai', String(fport)], { cwd: proj, env }, /fake OpenAI su (\S+)/);
  const fakeUrl = fake2.m[1];

  const app = await startProc('python3', ['run_webapp.py', '--no-browser'], { cwd: proj, env }, /Apri:\s+(http:\/\/\S+)/);
  const loginUrl = app.m[1];
  const baseUrl = loginUrl.replace(/\/#t=.*/, '');
  console.log('webapp:', loginUrl.replace(/#t=.*/, '#t=***'));

  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
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
    assert((await page.locator('#summary').textContent()).includes('Nessun nodo GPU'), '3D: avviso "nessun nodo GPU" nel riepilogo');
    await page.screenshot({ path: path.join(shots, '6_3d_riepilogo.png'), fullPage: true });
    await page.click('#btn-back3'); await page.click('#btn-back2');
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
      await page.waitForSelector('#jobs .job:first-child .tag.ok', { timeout: 40000 });
      await page.click('#jobs .job:first-child');
      await page.waitForSelector('#filebtns button', { timeout: 15000 });
      const labels = await page.locator('#filebtns button').allTextContents();
      assert(labels.some(l => l.includes('camera_poses.json')) && labels.some(l => l.includes('predictions.npz')), '3D: pulsanti di download: ' + labels.map(l => l.replace('⬇ ', '').split(' ')[0]).join(', '));
      const [dl3] = await Promise.all([page.waitForEvent('download'), page.locator('#filebtns button', { hasText: 'camera_poses.json' }).click()]);
      const poses = JSON.parse(fs.readFileSync(await dl3.path(), 'utf8'));
      assert(poses.frames.length === 4 && dl3.suggestedFilename() === 'camera_poses.json', '3D: file scaricato dal browser (4 pose)');
      await page.screenshot({ path: path.join(shots, '7_3d_risultati.png'), fullPage: true });
    } finally { gpu.kill(); }

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
    app.p.kill(); fake2.p.kill();
  }
})();
