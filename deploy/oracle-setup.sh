#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  ALFA – installazione su un server Linux GIÀ IN USO (es. Oracle Cloud Free
#  Tier con altre app come navetta e FreeLLMAPI).
#
#  Principio: non toccare nulla di ciò che esiste già.
#    • Node.js 22 privato in /opt/alfa-node (il Node di sistema resta com'è)
#    • porta interna libera scelta automaticamente (da 3080 in su)
#    • reverse proxy: rileva cosa occupa già le porte 80/443
#        - Caddy  → aggiunge SOLO un blocco per il dominio di ALFA (con backup)
#        - nginx  → aggiunge SOLO /etc/nginx/conf.d/alfa.conf + certificato certbot
#        - nulla  → installa Caddy con HTTPS automatico
#        - altro (Docker, Traefik…) → non tocca niente e stampa le istruzioni
#    • ogni modifica a configurazioni esistenti viene validata; in caso di
#      errore si ripristina il backup.
#
#  Uso (dalla cartella del progetto):
#     sudo bash deploy/oracle-setup.sh traduttore.tuodominio.it
#  Opzionale: CERT_EMAIL=tu@esempio.it per le notifiche Let's Encrypt (nginx).
#  Si può rilanciare senza problemi (idempotente).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DOMAIN="${1:-}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${SUDO_USER:-$(stat -c %U "$APP_DIR")}"
NODE_DIR=/opt/alfa-node
UNIT=/etc/systemd/system/alfa.service
MARK_BEGIN="# >>> ALFA (gestito da deploy/oracle-setup.sh)"
MARK_END="# <<< ALFA"

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Esegui con sudo: sudo bash deploy/oracle-setup.sh <dominio>"
[[ -n "$DOMAIN" ]] || fail "Indica il dominio, es: sudo bash deploy/oracle-setup.sh traduttore.tuodominio.it"
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || fail "Dominio non valido: $DOMAIN"
[[ -f "$APP_DIR/server/index.js" ]] || fail "Non trovo server/index.js in $APP_DIR"

if command -v apt-get >/dev/null; then PKG=apt; elif command -v dnf >/dev/null; then PKG=dnf; else fail "Serve apt o dnf"; fi
pkg_install() {
  if [[ $PKG == apt ]]; then DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"; else dnf install -y "$@"; fi
}

case "$(uname -m)" in
  x86_64)  NODE_ARCH=x64;   CADDY_ARCH=amd64 ;;
  aarch64) NODE_ARCH=arm64; CADDY_ARCH=arm64 ;;
  *) fail "Architettura non supportata: $(uname -m)" ;;
esac

say "Controllo i prerequisiti"
for bin in curl tar xz ss; do
  command -v "$bin" >/dev/null || { [[ $PKG == apt ]] && apt-get update -y >/dev/null; pkg_install curl tar xz-utils iproute2 2>/dev/null || pkg_install curl tar xz iproute; break; }
done

# ── Node.js 22 privato (non tocca il Node usato dalle altre app) ─────────────
if [[ ! -x "$NODE_DIR/bin/node" ]] || (( $("$NODE_DIR/bin/node" -p 'process.versions.node.split(".")[0]') < 22 )); then
  say "Installo Node.js 22 privato in $NODE_DIR"
  BASE=https://nodejs.org/dist/latest-v22.x
  TMP="$(mktemp -d)"
  curl -fsSL "$BASE/SHASUMS256.txt" -o "$TMP/SHASUMS256.txt"
  FILE="$(grep -oE "node-v[0-9.]+-linux-${NODE_ARCH}\.tar\.xz" "$TMP/SHASUMS256.txt" | head -1)"
  [[ -n "$FILE" ]] || fail "Pacchetto Node.js non trovato"
  curl -fsSL "$BASE/$FILE" -o "$TMP/$FILE"
  (cd "$TMP" && grep " $FILE\$" SHASUMS256.txt | sha256sum -c - >/dev/null) || fail "Checksum Node.js non valido"
  rm -rf "$NODE_DIR" && mkdir -p "$NODE_DIR"
  tar -xJf "$TMP/$FILE" -C "$NODE_DIR" --strip-components=1
  rm -rf "$TMP"
fi
NODE_BIN="$NODE_DIR/bin/node"
say "Node.js $("$NODE_BIN" -v) (privato) pronto"

# ── Porta interna libera ─────────────────────────────────────────────────────
port_busy() { ss -Hltn "sport = :$1" | grep -q .; }
APP_PORT=""
if [[ -f $UNIT ]]; then APP_PORT="$(grep -oP '^Environment=PORT=\K[0-9]+' "$UNIT" || true)"; fi
if [[ -z "$APP_PORT" ]]; then
  APP_PORT=3080
  while port_busy "$APP_PORT"; do APP_PORT=$((APP_PORT + 1)); done
fi
say "ALFA userà la porta interna $APP_PORT (solo 127.0.0.1)"

# ── Configurazione e servizio dell'app ───────────────────────────────────────
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chown "$APP_USER": "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"

cat > "$UNIT" <<UNITFILE
[Unit]
Description=ALFA traduttore vocale
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=NODE_ENV=production
Environment=HOST=127.0.0.1
Environment=PORT=$APP_PORT
ExecStart=$NODE_BIN server/index.js
Restart=always
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNITFILE

systemctl daemon-reload
systemctl enable alfa >/dev/null 2>&1
systemctl restart alfa
for _ in 1 2 3 4 5; do curl -fsS "http://127.0.0.1:$APP_PORT/api/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$APP_PORT/api/health" >/dev/null || fail "ALFA non risponde: journalctl -u alfa -n 50"
say "ALFA attiva su 127.0.0.1:$APP_PORT ✔"

# ── Reverse proxy: chi occupa già 80/443? ────────────────────────────────────
LISTENER="$(ss -Hltnp '( sport = :443 or sport = :80 )' | grep -oP 'users:\(\("\K[^"]+' | sort -u | head -1 || true)"
say "Porte 80/443 occupate da: ${LISTENER:-nessuno}"

caddy_block() {
  cat <<BLOCK
$MARK_BEGIN
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$APP_PORT {
		flush_interval -1
	}
}
$MARK_END
BLOCK
}

nginx_conf() {
  cat <<CONF
$MARK_BEGIN
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        # streaming delle traduzioni (SSE): nessun buffering
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 120s;
    }
}
$MARK_END
CONF
}

# Sostituisce (o aggiunge) il blocco ALFA in un file, lasciando intatto il resto
upsert_block() {
  local file="$1" content="$2"
  if grep -qF "$MARK_BEGIN" "$file"; then
    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '$0==b{skip=1} !skip{print} $0==e{skip=0}' "$file" > "$file.alfa-tmp"
    mv "$file.alfa-tmp" "$file"
  fi
  printf '\n%s\n' "$content" >> "$file"
}

open_firewall() {
  say "Apro le porte 80 e 443 nel firewall del sistema"
  if command -v firewall-cmd >/dev/null && systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --add-service=http --add-service=https && firewall-cmd --reload
  elif command -v iptables >/dev/null; then
    for port in 80 443; do
      iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null \
        || iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
    done
    if command -v netfilter-persistent >/dev/null; then netfilter-persistent save
    elif [[ -d /etc/iptables ]]; then iptables-save > /etc/iptables/rules.v4; fi
  fi
}

install_caddy() {
  say "Installo Caddy ($CADDY_ARCH)"
  if ! curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=${CADDY_ARCH}" -o /usr/local/bin/caddy; then
    local tag tmp
    tag="$(curl -fsSL https://api.github.com/repos/caddyserver/caddy/releases/latest | grep -oE '"tag_name": *"v[^"]+"' | grep -oE 'v[0-9.]+')"
    tmp="$(mktemp -d)"
    curl -fsSL "https://github.com/caddyserver/caddy/releases/download/${tag}/caddy_${tag#v}_linux_${CADDY_ARCH}.tar.gz" -o "$tmp/c.tgz"
    tar -xzf "$tmp/c.tgz" -C "$tmp" caddy && mv "$tmp/caddy" /usr/local/bin/caddy && rm -rf "$tmp"
  fi
  chmod +x /usr/local/bin/caddy
  /usr/local/bin/caddy version >/dev/null || fail "Download di Caddy non valido"
  id caddy >/dev/null 2>&1 || useradd --system --home /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy
  mkdir -p /etc/caddy && touch /etc/caddy/Caddyfile
  cat > /etc/systemd/system/caddy.service <<UNITFILE
[Unit]
Description=Caddy reverse proxy (HTTPS)
After=network-online.target
Wants=network-online.target

[Service]
User=caddy
Group=caddy
Environment=HOME=/var/lib/caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNITFILE
}

PROXY_RESULT=""
case "$LISTENER" in
  "")
    install_caddy
    upsert_block /etc/caddy/Caddyfile "$(caddy_block)"
    /usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null || fail "Caddyfile non valido"
    open_firewall
    systemctl daemon-reload
    systemctl enable --now caddy
    systemctl restart caddy
    PROXY_RESULT="Caddy installato: HTTPS automatico per $DOMAIN"
    ;;

  caddy)
    CADDYFILE=/etc/caddy/Caddyfile
    CADDY_BIN="$(command -v caddy || echo /usr/local/bin/caddy)"
    [[ -f $CADDYFILE ]] || fail "Caddy è attivo ma non trovo $CADDYFILE: aggiungi a mano il blocco qui sotto.$(printf '\n')$(caddy_block)"
    BACKUP="$CADDYFILE.bak-alfa-$(date +%Y%m%d%H%M%S)"
    cp -a "$CADDYFILE" "$BACKUP"
    upsert_block "$CADDYFILE" "$(caddy_block)"
    if ! "$CADDY_BIN" validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
      cp -a "$BACKUP" "$CADDYFILE"
      fail "La configurazione di Caddy non è valida: ripristinato il backup, nulla è cambiato."
    fi
    systemctl reload caddy || { cp -a "$BACKUP" "$CADDYFILE"; systemctl reload caddy || true; fail "Reload di Caddy fallito: backup ripristinato."; }
    PROXY_RESULT="Aggiunto il blocco ALFA al Caddyfile esistente (backup: $BACKUP)"
    ;;

  nginx)
    CONF=/etc/nginx/conf.d/alfa.conf
    [[ -d /etc/nginx/conf.d ]] || fail "nginx attivo ma /etc/nginx/conf.d non esiste"
    HAD_CONF=0; [[ -f $CONF ]] && HAD_CONF=1 && cp -a "$CONF" "$CONF.bak"
    # Se certbot ha già configurato HTTPS in passato non sovrascriviamo il file
    if [[ $HAD_CONF -eq 0 ]] || ! grep -q "ssl_certificate" "$CONF"; then
      nginx_conf > "$CONF"
    fi
    if ! nginx -t >/dev/null 2>&1; then
      if [[ $HAD_CONF -eq 1 ]]; then cp -a "$CONF.bak" "$CONF"; else rm -f "$CONF"; fi
      fail "Configurazione nginx non valida: ripristinata, nulla è cambiato."
    fi
    systemctl reload nginx
    if ! command -v certbot >/dev/null; then
      say "Installo certbot"
      pkg_install certbot python3-certbot-nginx || warn "Installa certbot a mano per l'HTTPS"
    fi
    if command -v certbot >/dev/null; then
      EMAIL_ARGS=(--register-unsafely-without-email)
      [[ -n "${CERT_EMAIL:-}" ]] && EMAIL_ARGS=(-m "$CERT_EMAIL")
      if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect "${EMAIL_ARGS[@]}"; then
        PROXY_RESULT="nginx: aggiunto $CONF con certificato HTTPS Let's Encrypt"
      else
        PROXY_RESULT="nginx: aggiunto $CONF, ma il certificato non è stato ottenuto (DNS non ancora propagato?). Rilancia: sudo certbot --nginx -d $DOMAIN"
      fi
    else
      PROXY_RESULT="nginx: aggiunto $CONF (solo HTTP). Serve certbot per l'HTTPS."
    fi
    ;;

  *)
    warn "Le porte 80/443 sono gestite da \"$LISTENER\" (es. Docker/Traefik): non modifico nulla."
    cat <<INFO

Aggiungi al tuo reverse proxy una regola per $DOMAIN verso ALFA.
ALFA ascolta su 127.0.0.1:$APP_PORT. Se il proxy gira in un container Docker, quell'indirizzo
non è raggiungibile: modifica $UNIT mettendo Environment=HOST=0.0.0.0, poi
usa http://172.17.0.1:$APP_PORT (o host.docker.internal) come destinazione e
NON aprire la porta $APP_PORT nella Security List di Oracle.

Esempio per Caddy:
$(caddy_block)

Esempio per nginx:
$(nginx_conf)
INFO
    PROXY_RESULT="Proxy da configurare a mano (vedi sopra)"
    ;;
esac

PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org || echo '<IP pubblico>')"
cat <<DONE

✅ Fatto. $PROXY_RESULT

Le altre app del server non sono state toccate (Node di sistema, porte, servizi).

Se non l'hai già fatto:
  • register.it → DNS: record A  "${DOMAIN%%.*}"  →  $PUBLIC_IP
  • Oracle Console → Security List: porte 80 e 443 aperte (probabilmente lo sono già)
Poi apri: https://$DOMAIN

Configurazione traduzione:  nano $APP_DIR/.env   →   sudo systemctl restart alfa
Log:  sudo journalctl -u alfa -f
DONE
