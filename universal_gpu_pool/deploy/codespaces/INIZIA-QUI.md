# ✅ Ci sei quasi — 3 tocchi

1. Cerca la scheda **PORTE** (in inglese **PORTS**, in basso: sul telefono potrebbe stare nel menu ☰ → *View* → *Ports*).
2. Sulla riga **8765 · GPU Pool** tocca l'**icona del globo 🌐** (*Open in Browser*).
3. Si apre il sito: **si attiva da solo**. Collega **Kaggle** quando te lo chiede e sei pronto.

Se compare una notifica «*Your application running on port 8765 is available*», tocca **Open in Browser** e hai già finito.

### Da sapere
- Tieni aperta la scheda mentre un lavoro gira: dopo circa 30 minuti senza attività GitHub spegne il computer in cloud
  (si allunga fino a 4 ore in github.com/settings/codespaces → *Default idle timeout*). Un lavoro già partito su Kaggle continua comunque.
- È gratis: GitHub dà a ogni account circa **60 ore al mese** di questo computer in cloud (120 «ore-core»). Quando non lo usi, **spegnilo**
  (github.com/codespaces → ⋯ → *Stop codespace*).
- **Non rendere pubblica la porta 8765** (resta «Private»): così lo apri solo tu, con il tuo account GitHub.
- Se il sito non si apre: nel terminale scrivi `bash universal_gpu_pool/avvia-codespaces.sh` e premi Invio; l'indirizzo compare alla fine.
