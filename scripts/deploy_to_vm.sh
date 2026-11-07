#!/usr/bin/env bash
# Copy this project to a fresh Vultr Ubuntu 24.04 VM and install it (no GitHub needed).
#   bash scripts/deploy_to_vm.sh <VM_IP>
# Re-running is safe: it syncs the code and re-runs the idempotent setup.
set -euo pipefail
IP="${1:?usage: deploy_to_vm.sh <VM_IP>}"
KEY="${SSH_KEY:-$HOME/.ssh/vultr_blastproof}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new root@"$IP")
cd "$(dirname "$0")/.."

echo "==> Copying project to $IP:/opt/blastproof"
"${SSH[@]}" "mkdir -p /opt/blastproof"
rsync -az --delete -e "ssh -i $KEY -o StrictHostKeyChecking=accept-new" \
  --exclude .venv --exclude var --exclude .env --exclude .git --exclude __pycache__ --exclude .pytest_cache \
  ./ root@"$IP":/opt/blastproof/

echo "==> Running setup on the VM (5-8 minutes on first run)"
if [ -f .env ] && grep -q "^VULTR_INFERENCE_API_KEY=." .env; then
  # Send the key over SSH stdin, never on the command line.
  grep "^VULTR_INFERENCE_API_KEY=" .env | cut -d= -f2- | "${SSH[@]}" \
    'read -r K; VULTR_INFERENCE_API_KEY="$K" bash /opt/blastproof/deploy/setup.sh'
else
  "${SSH[@]}" -t "bash /opt/blastproof/deploy/setup.sh"
fi
