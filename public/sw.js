/**
 * Service Worker di ALFA.
 *
 * Strategie:
 *  - App shell (HTML/JS/CSS/icone): precaricata all'installazione,
 *    servita "stale-while-revalidate" → avvio istantaneo anche offline.
 *  - Navigazioni: network-first con fallback alla shell in cache.
 *  - /api/*: SEMPRE rete, mai in cache (traduzioni e audio sono dinamici).
 *
 * Per pubblicare una nuova versione basta incrementare VERSION.
 */
const VERSION = 'alfa-v1.1.0';
const SHELL = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/css/styles.css',
  '/js/app.js',
  '/js/api.js',
  '/js/conversation.js',
  '/js/languages.js',
  '/js/settings.js',
  '/js/text-utils.js',
  '/js/tts.js',
  '/js/ui.js',
  '/js/wakelock.js',
  '/js/stt/audio-utils.js',
  '/js/stt/cloud.js',
  '/js/stt/pcm-worklet.js',
  '/js/stt/segmenter.js',
  '/js/stt/webspeech.js',
  '/icons/icon.svg',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-maskable-512.png',
  '/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      .then((cache) => cache.addAll(SHELL.map((u) => new Request(u, { cache: 'reload' }))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Solo GET della stessa origine; le API non passano mai dalla cache.
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((res) => {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put('/index.html', copy));
          return res;
        })
        .catch(() => caches.match('/index.html')),
    );
    return;
  }

  // Stale-while-revalidate per gli asset statici
  event.respondWith(
    caches.open(VERSION).then(async (cache) => {
      const cached = await cache.match(request, { ignoreSearch: true });
      const network = fetch(request)
        .then((res) => {
          if (res.ok) cache.put(request, res.clone());
          return res;
        })
        .catch(() => cached);
      return cached || network;
    }),
  );
});
