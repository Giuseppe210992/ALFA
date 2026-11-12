/**
 * Mantiene lo schermo acceso durante la conversazione (Screen Wake Lock API).
 * Se lo schermo si spegne, il browser sospende microfono e sintesi vocale:
 * per un uso hands-free è essenziale. Il lock viene ri-acquisito quando la
 * pagina torna visibile.
 */
let lock = null;
let wanted = false;

async function acquire() {
  if (!wanted || !('wakeLock' in navigator) || document.visibilityState !== 'visible') return;
  try {
    lock = await navigator.wakeLock.request('screen');
    lock.addEventListener('release', () => { lock = null; });
  } catch { /* non supportato o batteria scarica */ }
}

document.addEventListener('visibilitychange', () => {
  if (wanted && !lock) acquire();
});

export function keepAwake(on) {
  wanted = on;
  if (on) acquire();
  else if (lock) lock.release().catch(() => {});
}
