#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  ALFA – installazione su server Oracle Cloud Free Tier (o qualsiasi VPS Linux)
#
#  Installa e configura:
#    • Node.js 22 (binari ufficiali, x86_64 o ARM Ampere)
#    • servizio systemd "alfa" (avvio automatico e riavvio in caso di crash)
#    • Caddy come reverse proxy con HTTPS automatico (Let's Encrypt)
#    • firewall del sistema operativo (porte 80 e 443)
#
#  Uso (dalla cartella del progetto clonato):
#     sudo bash deploy/oracle-setup.sh traduttore.tuodominio.it
#
#  Si può rilanciare senza problemi (idempotente).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DOMAIN="${1:-}"
APP_PORT=3000
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${SUDO_USER:-$(stat -c %U "$APP_DIR")}"

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Esegui con sudo: sudo bash deploy/oracle-setup.sh $DOMAIN"
[[ -n "$DOMAIN" ]] || fail "Indica il dominio, es: sudo bash deploy/oracle-setup.sh traduttore.tuodominio.it"
[[ -f "$APP_DIR/server/index.js" ]] || fail "Non trovo server/index.js in $APP_DIR"

# ── Pacchetti di base ────────────────────────────────────────────────────────
say "Installo i pacchetti di base"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y curl tar xz-utils git ca-certificates
elif command -v dnf >/dev/null; then
  dnf install -y curl tar xz git ca-certificates
else
  fail "Distribuzione non supportata (serve apt o dnf)"
fi

case "$(uname -m)" in
  x86_64)  NODE_ARCH=x64;   CADDY_ARCH=amd64 ;;
  aarch64) NODE_ARCH=arm64; CADDY_ARCH=arm64 ;;
  *) fail "Architettura non supportata: $(uname -m)" ;;
esac

# ── Node.js 22 ───────────────────────────────────────────────────────────────
NODE_MAJOR="$( (command -v node >/dev/null && node -p 'process.versions.node.split(".")[0]') || echo 0)"
if (( NODE_MAJOR < 22 )); then
  say "Installo Node.js 22 ($NODE_ARCH)"
  BASE=https://nodejs.org/dist/latest-v22.x
  TMP="$(mktemp -d)"
  curl -fsSL "$BASE/SHASUMS256.txt" -o "$TMP/SHASUMS256.txt"
  FILE="$(grep -oE "node-v[0-9.]+-linux-${NODE_ARCH}\.tar\.xz" "$TMP/SHASUMS256.txt" | head -1)"
  [[ -n "$FILE" ]] || fail "Impossibile trovare il pacchetto Node.js"
  curl -fsSL "$BASE/$FILE" -o "$TMP/$FILE"
  (cd "$TMP" && grep " $FILE\$" SHASUMS256.txt | sha256sum -c -) || fail "Checksum di Node.js non valido"
  rm -rf /opt/node && mkdir -p /opt/node
  tar -xJf "$TMP/$FILE" -C /opt/node --strip-components=1
  ln -sf /opt/node/bin/node /usr/local/bin/node
  ln -sf /opt/node/bin/npm /usr/local/bin/npm
  rm -rf "$TMP"
fi
NODE_BIN="$(command -v node)"
say "Node.js $("$NODE_BIN" -v) pronto"

# ── Configurazione dell'app ──────────────────────────────────────────────────
if [[ ! -f "$APP_DIR/.env" ]]; then
  say "Creo .env dal modello (.env.example)"
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chown "$APP_USER": "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"

# ── Servizio systemd dell'app ────────────────────────────────────────────────
say "Configuro il servizio systemd 'alfa'"
cat > /etc/systemd/system/alfa.service <<UNIT
[Unit]
Description=ALFA traduttore vocale
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
# HOST=127.0.0.1: l'app è raggiungibile solo tramite Caddy (HTTPS)
Environment=NODE_ENV=production
Environment=HOST=127.0.0.1
Environment=PORT=$APP_PORT
ExecStart=$NODE_BIN server/index.js
Restart=always
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

# ── Caddy (HTTPS automatico) ─────────────────────────────────────────────────
if ! command -v caddy >/dev/null; then
  say "Installo Caddy ($CADDY_ARCH)"
  if ! curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=${CADDY_ARCH}" -o /usr/local/bin/caddy; then
    # Alternativa: ultima release ufficiale da GitHub
    TAG="$(curl -fsSL https://api.github.com/repos/caddyserver/caddy/releases/latest | grep -oE '"tag_name": *"v[^"]+"' | grep -oE 'v[0-9.]+')"
    TMP="$(mktemp -d)"
    curl -fsSL "https://github.com/caddyserver/caddy/releases/download/${TAG}/caddy_${TAG#v}_linux_${CADDY_ARCH}.tar.gz" -o "$TMP/caddy.tgz"
    tar -xzf "$TMP/caddy.tgz" -C "$TMP" caddy
    mv "$TMP/caddy" /usr/local/bin/caddy
    rm -rf "$TMP"
  fi
  chmod +x /usr/local/bin/caddy
  /usr/local/bin/caddy version >/dev/null || fail "Download di Caddy non valido"
fi
CADDY_BIN="$(command -v caddy)"
id caddy >/dev/null 2>&1 || useradd --system --home /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy
mkdir -p /etc/caddy

say "Configuro Caddy per https://$DOMAIN"
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$APP_PORT {
		# Inoltro immediato dello streaming delle traduzioni (SSE)
		flush_interval -1
	}
}
CADDY

cat > /etc/systemd/system/caddy.service <<UNIT
[Unit]
Description=Caddy reverse proxy (HTTPS)
After=network-online.target
Wants=network-online.target

[Service]
User=caddy
Group=caddy
Environment=HOME=/var/lib/caddy
ExecStart=$CADDY_BIN run --environ --config /etc/caddy/Caddyfile
ExecReload=$CADDY_BIN reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

# ── Firewall del sistema operativo ───────────────────────────────────────────
say "Apro le porte 80 e 443 nel firewall del sistema"
if command -v firewall-cmd >/dev/null && systemctl is-active --quiet firewalld; then
  # Oracle Linux
  firewall-cmd --permanent --add-service=http --add-service=https
  firewall-cmd --reload
elif command -v iptables >/dev/null; then
  # Ubuntu su Oracle: le regole predefinite rifiutano tutto tranne SSH
  for port in 80 443; do
    iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null \
      || iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
  done
  if command -v netfilter-persistent >/dev/null; then
    netfilter-persistent save
  elif [[ -d /etc/iptables ]]; then
    iptables-save > /etc/iptables/rules.v4
  fi
fi

# ── Avvio ────────────────────────────────────────────────────────────────────
say "Avvio i servizi"
systemctl daemon-reload
systemctl enable --now alfa
systemctl restart alfa
systemctl enable --now caddy
systemctl restart caddy

sleep 2
if curl -fsS "http://127.0.0.1:$APP_PORT/api/health" >/dev/null; then
  say "App attiva in locale ✔"
else
  fail "L'app non risponde: controlla con  journalctl -u alfa -n 50"
fi

PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org || echo '<IP pubblico>')"
cat <<DONE

✅ Installazione completata.

Ultimi controlli (se non già fatti):
  1. Oracle Cloud Console → la tua istanza → Subnet → Security List →
     "Add Ingress Rules": sorgente 0.0.0.0/0, TCP, porte 80 e 443.
  2. register.it → DNS: record A  nome "${DOMAIN%%.*}"  valore $PUBLIC_IP
  3. Dopo la propagazione DNS apri: https://$DOMAIN
     (Caddy ottiene il certificato HTTPS da solo, al primo accesso).

Comandi utili:
  sudo journalctl -u alfa -f       # log dell'app
  sudo journalctl -u caddy -f      # log HTTPS/proxy
  nano $APP_DIR/.env               # chiavi API, poi:  sudo systemctl restart alfa
  bash $APP_DIR/deploy/update.sh   # aggiorna all'ultima versione
DONE
