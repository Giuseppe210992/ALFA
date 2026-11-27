// Service worker minimo: rende l'app installabile e mostra la schermata anche se la rete cade un attimo.
// Non mette MAI in cache le chiamate API (token, job, risultati): solo la pagina di avvio.
const CACHE = "gpool-shell-v1";

self.addEventListener("install", (e) => { self.skipWaiting(); });

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.mode !== "navigate" || req.method !== "GET") return;     // tutto il resto va dritto in rete
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put("/", copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match("/").then((hit) => hit || new Response(
        "<meta charset=utf-8><meta name=viewport content='width=device-width'><body style='font-family:sans-serif;background:#0b0f19;color:#eef2ff;padding:2em'>" +
        "<h2>Nessuna connessione</h2><p>Il server non risponde. Se usi Codespaces, riaccendilo da GitHub e riapri l'app.</p>",
        { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } })))
  );
});
