#!/usr/bin/env bash
# Aggiorna ALFA all'ultima versione del ramo corrente e riavvia il servizio.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git pull --ff-only
sudo systemctl restart alfa
echo "✅ ALFA aggiornata: $(git log -1 --format='%h %s')"
