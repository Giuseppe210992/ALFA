#!/usr/bin/env bash
# Installa GPU Pool su una VM Oracle (Ubuntu o Oracle Linux, ARM o AMD) con HTTPS automatico e indirizzo fisso.
# Si lancia DENTRO l'istanza:   curl -fsSL <indirizzo-di-questo-file> | sudo bash
# Rilanciarlo aggiorna il programma senza perdere chiavi, token e risultati.
# Variabili opzionali: GH_TOKEN (repository privato), GPOOL_BRANCH, GPOOL_REPO, GPOOL_HOST (dominio tuo al posto di IP.sslip.io)
set -euo pipefail

REPO="${GPOOL_REPO:-Giuseppe210992/ALFA}"
BRANCH="${GPOOL_BRANCH:-claude/new-session-q8o2nc}"
BASE=/opt/gpool
APP=$BASE/app
ENVF=/etc/gpool.env
[ "$(id -u)" = 0 ] || { echo "Lancialo con sudo."; exit 1; }

say() { printf '\n==> %s\n' "$*"; }

say "0/6 Controllo porte e memoria"
if command -v ss >/dev/null; then
  BUSY=$(ss -ltnp 2>/dev/null | awk '$4 ~ /:(80|443)$/ && $0 !~ /caddy/ {print $4" "$NF}')
  if [ -n "$BUSY" ]; then
    echo "Le porte 80/443 sono già usate da altri programmi sull'istanza:"; echo "$BUSY"
    echo "Libera le porte, oppure usa il tuo proxy esistente verso 127.0.0.1:8765 e lancia con GPOOL_NO_CADDY=1."
    [ -n "${GPOOL_NO_CADDY:-}" ] || exit 1
  fi
fi
free -m 2>/dev/null | awk '/^Mem:/ {print "  memoria libera: " $7 " MB"}'

say "1/6 Pacchetti di base"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq python3 curl ca-certificates tar >/dev/null
elif command -v dnf >/dev/null; then
  dnf install -y -q python3 curl ca-certificates tar >/dev/null
else
  echo "Sistema non riconosciuto (serve apt o dnf)."; exit 1
fi
python3 -c 'import sys; assert sys.version_info>=(3,8)' || { echo "Serve Python 3.8+"; exit 1; }

say "2/6 Scarico il programma"
id gpool >/dev/null 2>&1 || useradd --system --create-home --home-dir $BASE --shell /usr/sbin/nologin gpool
mkdir -p $APP
TMP=$(mktemp -d)
AUTH=(); [ -n "${GH_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $GH_TOKEN")
if ! curl -fsSL "${AUTH[@]}" "https://api.github.com/repos/$REPO/tarball/$BRANCH" -o "$TMP/src.tgz"; then
  echo "Download non riuscito. Se il repository è privato, rilancia con:  GH_TOKEN=il_tuo_token sudo -E bash install.sh"; exit 1
fi
mkdir "$TMP/x" && tar -xzf "$TMP/src.tgz" -C "$TMP/x"
SRC=$(ls -d "$TMP"/x/*/universal_gpu_pool)
cp -r "$SRC"/. $APP/          # sovrascrive il codice; config/ locale e gpu_pool_store restano
rm -rf "$TMP" $APP/tests
chown -R gpool:gpool $BASE

say "3/6 Indirizzo e codice di accesso"
IP=$(curl -fsS https://api.ipify.org 2>/dev/null || curl -fsS https://ifconfig.me 2>/dev/null || true)
[ -n "$IP" ] || { echo "Non riesco a leggere l'IP pubblico."; exit 1; }
HOST="${GPOOL_HOST:-$(echo "$IP" | tr . -).sslip.io}"
if [ ! -f $ENVF ]; then
  TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
  printf 'GPOOL_PUBLIC=1\nGPOOL_KEEP_LOCAL=1\nGPOOL_PORT=8765\nGPOOL_TOKEN=%s\n' "$TOKEN" > $ENVF
  chmod 600 $ENVF
fi
TOKEN=$(grep '^GPOOL_TOKEN=' $ENVF | cut -d= -f2-)

say "4/6 Servizio sempre acceso"
cat > /etc/systemd/system/gpool.service <<UNIT
[Unit]
Description=GPU Pool
After=network-online.target
Wants=network-online.target
[Service]
User=gpool
WorkingDirectory=$APP
EnvironmentFile=$ENVF
ExecStart=/usr/bin/python3 run_webapp.py --host 127.0.0.1 --port 8765 --no-browser
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT

say "5/6 HTTPS automatico (Caddy)"
if ! command -v caddy >/dev/null; then
  case "$(uname -m)" in aarch64|arm64) A=arm64;; *) A=amd64;; esac
  curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=$A" -o /usr/local/bin/caddy
  chmod +x /usr/local/bin/caddy
fi
mkdir -p /etc/caddy
printf '%s {\n  reverse_proxy 127.0.0.1:8765\n}\n' "$HOST" > /etc/caddy/Caddyfile
cat > /etc/systemd/system/gpool-caddy.service <<UNIT
[Unit]
Description=HTTPS per GPU Pool
After=network-online.target
[Service]
Environment=HOME=/var/lib/caddy
StateDirectory=caddy
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
[Install]
WantedBy=multi-user.target
UNIT

say "6/6 Porte del firewall dell'istanza (80 e 443)"
if command -v firewall-cmd >/dev/null && systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-service=http --add-service=https >/dev/null && firewall-cmd --reload >/dev/null
elif command -v iptables >/dev/null; then
  for p in 80 443; do
    iptables -C INPUT -p tcp --dport $p -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport $p -j ACCEPT
  done
  command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null 2>&1 || true
fi

systemctl daemon-reload
systemctl enable --now gpool.service >/dev/null 2>&1
[ -n "${GPOOL_NO_CADDY:-}" ] || systemctl enable --now gpool-caddy.service >/dev/null 2>&1
systemctl restart gpool.service
sleep 3
systemctl is-active --quiet gpool.service && echo "  programma: attivo" || { echo "  programma NON attivo:"; journalctl -u gpool -n 15 --no-pager; exit 1; }

cat <<MSG

==============================================================
  FATTO. Il tuo indirizzo fisso (HTTPS):

     https://$HOST/#t=$TOKEN

  Aprilo dal telefono: l'app ti riconosce. Poi tocca «Installa»
  per mettere l'icona sulla schermata Home.
  NON condividere questo link: contiene il tuo codice di accesso.

  Se la pagina non si apre: nella console Oracle apri
  Rete (VCN) → Security List → Aggiungi regola in ingresso:
  origine 0.0.0.0/0, TCP, porte 80 e 443.
==============================================================
MSG
