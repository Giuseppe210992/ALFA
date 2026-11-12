/**
 * AudioWorklet che cattura il PCM grezzo del microfono e lo invia al thread
 * principale in blocchi da 2048 campioni (~43 ms a 48 kHz).
 * Usare PCM (invece di MediaRecorder) permette tagli precisi al campione,
 * senza perdere audio tra un segmento e l'altro, e funziona anche su iOS.
 */
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = 2048;
    this.buf = new Float32Array(this.size);
    this.pos = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      for (let i = 0; i < ch.length; i++) {
        this.buf[this.pos++] = ch[i];
        if (this.pos === this.size) {
          this.port.postMessage(this.buf);
          this.buf = new Float32Array(this.size);
          this.pos = 0;
        }
      }
    }
    return true; // mantieni vivo il processore
  }
}

registerProcessor('pcm-capture', PcmCapture);
