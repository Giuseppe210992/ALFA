/**
 * Genera le icone PNG della PWA a partire da public/icons/icon.svg.
 * Richiede Playwright (solo in sviluppo):  npx playwright@latest install chromium
 * Uso: npm run icons
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const dir = join(dirname(fileURLToPath(import.meta.url)), '..', 'public', 'icons');
const svg = readFileSync(join(dir, 'icon.svg'), 'utf8');
const dataUrl = `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`;

// scale: frazione del lato occupata dal disegno; bg: sfondo pieno (icone maskable/iOS)
const targets = [
  { file: 'icon-192.png', size: 192, scale: 1, bg: 'transparent' },
  { file: 'icon-512.png', size: 512, scale: 1, bg: 'transparent' },
  // Maskable: il contenuto deve stare nella "safe zone" (cerchio dell'80%)
  { file: 'icon-maskable-512.png', size: 512, scale: 0.8, bg: '#0e1116' },
  // iOS arrotonda da sé gli angoli: niente trasparenza
  { file: 'apple-touch-icon.png', size: 180, scale: 1, bg: '#0e1116' },
];

const browser = await chromium.launch();
const page = await browser.newPage();
for (const t of targets) {
  await page.setViewportSize({ width: t.size, height: t.size });
  const inner = Math.round(t.size * t.scale);
  await page.setContent(`<html><body style="margin:0;background:${t.bg};display:grid;place-items:center;width:${t.size}px;height:${t.size}px">
    <img src="${dataUrl}" width="${inner}" height="${inner}"></body></html>`);
  await page.screenshot({ path: join(dir, t.file), omitBackground: t.bg === 'transparent' });
  console.log('✓', t.file);
}
await browser.close();
