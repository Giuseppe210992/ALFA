#!/usr/bin/env bash
# Avvio dentro GitHub Codespaces: parte da solo quando il codespace si accende (vedi .devcontainer/devcontainer.json).
cd "$(dirname "$0")" || exit 1
PORT="${GPOOL_PORT:-8765}"
if curl -fs "http://127.0.0.1:${PORT}/claim/status" >/dev/null 2>&1; then
  echo "GPU Pool è già in esecuzione."
else
  # GPOOL_PUBLIC: la copia si attiva da sola al primo accesso (la porta di Codespaces è privata: la apre solo il tuo account GitHub)
  # GPOOL_KEEP_LOCAL: i token salvati restano sul disco del codespace (non si azzera come sui server gratuiti)
  GPOOL_PUBLIC=1 GPOOL_KEEP_LOCAL=1 nohup python3 run_webapp.py --host 0.0.0.0 --port "$PORT" --no-browser > /tmp/gpool.log 2>&1 &
fi
for _ in $(seq 1 40); do
  curl -fs "http://127.0.0.1:${PORT}/claim/status" >/dev/null 2>&1 && break
  sleep 1
done
URL="https://${CODESPACE_NAME:-codespace}-${PORT}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}/"
# Pagina «formattata» con UN grande link: sul telefono l'editor di GitHub è inutilizzabile, qui basta un tocco.
cat > APRI-L-APP.md <<MD
# ✅ L'app è pronta

## 👉 [TOCCA QUI PER APRIRE L'APP](${URL})

Se il telefono chiede conferma di aprire un sito esterno, tocca **Apri**.

Nel sito: collega **Kaggle** quando te lo chiede e sei pronto. Si attiva da solo.

---

Se il link non si apre, copia questo indirizzo nel browser:

\`${URL}\`

*Non rendere pubblica la porta 8765. Quando hai finito, spegni il codespace da github.com/codespaces (⋯ → Stop codespace).*
MD
echo ""
echo "  ✅ GPU Pool è pronto."
echo "  Apri questo indirizzo:  ${URL}"
echo "  (oppure scheda PORTE → riga 8765 → icona del globo)"
echo ""
