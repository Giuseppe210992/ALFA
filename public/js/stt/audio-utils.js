/**
 * Utility audio pure: livello RMS, ricampionamento, codifica WAV e un VAD
 * (Voice Activity Detector) adattivo basato sull'energia.
 */

export function rms(samples) {
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / (samples.length || 1));
}

/** Ricampiona (media a blocchi + interpolazione lineare) a `toRate` Hz. */
export function downsample(samples, fromRate, toRate = 16000) {
  if (fromRate === toRate) return samples;
  const ratio = fromRate / toRate;
  const outLen = Math.floor(samples.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const start = i * ratio;
    const end = Math.min(samples.length, (i + 1) * ratio);
    let sum = 0;
    let n = 0;
    for (let j = Math.floor(start); j < end; j++) { sum += samples[j]; n++; }
    out[i] = n ? sum / n : 0;
  }
  return out;
}

/** Codifica PCM float [-1,1] in WAV 16 bit mono. */
export function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true); // dimensione chunk fmt
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bit per campione
  writeStr(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  let off = 44;
  for (let i = 0; i < samples.length; i++, off += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}

/** Concatena più Float32Array. */
export function concat(chunks) {
  const len = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Float32Array(len);
  let off = 0;
  for (const c of chunks) { out.set(c, off); off += c.length; }
  return out;
}

/**
 * VAD adattivo: stima il rumore di fondo quando nessuno parla e considera
 * "voce" ciò che supera di `ratio` volte quel livello.
 */
export class Vad {
  constructor({ ratio = 2.8, minRms = 0.006, startMs = 90, hangoverMs = 550 } = {}) {
    Object.assign(this, { ratio, minRms, startMs, hangoverMs });
    this.floor = 0.01;
    this.speaking = false;
    this.aboveMs = 0;
    this.belowMs = 0;
  }

  get threshold() {
    return Math.max(this.minRms, this.floor * this.ratio);
  }

  /**
   * @param {number} level  RMS del blocco
   * @param {number} ms     durata del blocco
   * @returns {'start'|'end'|null}
   */
  feed(level, ms) {
    const th = this.threshold;
    if (!this.speaking) {
      // Aggiorna il rumore di fondo solo in assenza di voce (EMA lenta)
      this.floor = Math.max(0.002, this.floor * 0.96 + Math.min(level, th) * 0.04);
      if (level > th) {
        this.aboveMs += ms;
        if (this.aboveMs >= this.startMs) {
          this.speaking = true;
          this.belowMs = 0;
          return 'start';
        }
      } else {
        this.aboveMs = 0;
      }
      return null;
    }
    // In voce: la fine richiede silenzio prolungato (hangover)
    if (level < th * 0.8) {
      this.belowMs += ms;
      if (this.belowMs >= this.hangoverMs) {
        this.speaking = false;
        this.aboveMs = 0;
        return 'end';
      }
    } else {
      this.belowMs = 0;
    }
    return null;
  }

  reset() {
    this.speaking = false;
    this.aboveMs = 0;
    this.belowMs = 0;
  }
}
