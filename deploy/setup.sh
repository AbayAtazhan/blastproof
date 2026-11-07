#!/usr/bin/env bash
# Blastproof: one-shot setup for a fresh Vultr Cloud Compute VM (Ubuntu 24.04).
#
#   git clone <repo> /opt/blastproof && sudo bash /opt/blastproof/deploy/setup.sh
#
# Optional environment variables:
#   VULTR_INFERENCE_API_KEY  inference key (otherwise you are prompted, or edit /opt/blastproof/.env later)
#   DOMAIN                   your domain; default: <public-ip>.sslip.io (automatic HTTPS, no DNS setup)
#   ENABLE_GVISOR=1          run sandboxes under gVisor (runsc). Set 0 to use plain runc.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR=/var/lib/blastproof
ENABLE_GVISOR="${ENABLE_GVISOR:-1}"
log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)."; exit 1; }
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
ARCH="$(dpkg --print-architecture)"

log "Base packages"
apt-get update -y
apt-get install -y ca-certificates curl gnupg git python3-venv python3-pip ufw

log "Docker Engine"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
fi
systemctl enable --now docker

RUNTIME=""
if [ "$ENABLE_GVISOR" = "1" ]; then
  log "gVisor (runsc): user-space kernel for the sandboxes"
  if ! command -v runsc >/dev/null; then
    curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor --yes -o /usr/share/keyrings/gvisor-archive-keyring.gpg
    echo "deb [arch=${ARCH} signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
      > /etc/apt/sources.list.d/gvisor.list
    apt-get update -y
    apt-get install -y runsc
  fi
  runsc install
  systemctl restart docker
  if docker run --rm --runtime=runsc --network none hello-world >/dev/null 2>&1; then
    RUNTIME="runsc"
  else
    echo "WARNING: gVisor self-test failed; falling back to runc."
  fi
fi

log "Caddy (HTTPS reverse proxy)"
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

log "Service user and data directory"
id blastproof >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin blastproof
usermod -aG docker blastproof
install -d -o blastproof -g blastproof -m 0755 "$DATA_DIR" "$DATA_DIR/data"

log "Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

log "Sandbox image"
docker build -t blastproof-sandbox:latest "$APP_DIR/sandbox"

log "Configuration (.env)"
ENV_FILE="$APP_DIR/.env"
[ -f "$ENV_FILE" ] || cp "$APP_DIR/.env.example" "$ENV_FILE"
setenv() {  # setenv KEY VALUE: replace or append
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
setenv SANDBOX_RUNTIME "$RUNTIME"
setenv TRUST_PROXY 1
setenv DATA_DIR "$DATA_DIR/data"
KEY="${VULTR_INFERENCE_API_KEY:-}"
if [ -z "$KEY" ] && ! grep -q "^VULTR_INFERENCE_API_KEY=." "$ENV_FILE" && [ -t 0 ]; then
  read -r -s -p "Vultr Inference API key (input hidden, Enter to skip): " KEY; echo
fi
[ -n "$KEY" ] && setenv VULTR_INFERENCE_API_KEY "$KEY"
chown root:blastproof "$ENV_FILE"
chmod 0640 "$ENV_FILE"

log "systemd service"
install -m 0644 "$APP_DIR/deploy/blastproof.service" /etc/systemd/system/blastproof.service
sed -i "s|/opt/blastproof|$APP_DIR|g" /etc/systemd/system/blastproof.service
systemctl daemon-reload
systemctl enable blastproof
systemctl restart blastproof

log "HTTPS"
if [ -z "${DOMAIN:-}" ]; then
  IP="$(hostname -I | awk '{print $1}')"
  DOMAIN="${IP//./-}.sslip.io"
fi
sed "s|{DOMAIN}|$DOMAIN|" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

log "Firewall"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

sleep 3
STATUS="$(curl -s http://127.0.0.1:8000/api/health || true)"
log "Done"
echo "  URL:      https://$DOMAIN"
echo "  Runtime:  ${RUNTIME:-runc}"
echo "  Health:   $STATUS"
echo "  Logs:     journalctl -u blastproof -f"
grep -q "^VULTR_INFERENCE_API_KEY=." "$ENV_FILE" || echo "  NOTE: add VULTR_INFERENCE_API_KEY to $ENV_FILE, then: systemctl restart blastproof"
