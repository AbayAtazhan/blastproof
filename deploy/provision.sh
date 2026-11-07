#!/usr/bin/env bash
# Create a Vultr Cloud Compute VM and install Blastproof on it through the Vultr API.
#
#   VULTR_API_KEY=... REPO_URL=https://github.com/<you>/blastproof bash deploy/provision.sh
#
# Optional: REGION (default sea), PLAN (default vc2-1c-2gb), SSH_KEY_ID (from `GET /v2/ssh-keys`).
# The inference key is deliberately NOT sent in user-data (user-data is readable from the
# instance metadata service). Add it afterwards over SSH; the script prints the command.
set -euo pipefail
: "${VULTR_API_KEY:?set VULTR_API_KEY (Vultr console -> Account -> API)}"
: "${REPO_URL:?set REPO_URL to the public git URL of this repository}"
REGION="${REGION:-sea}"
PLAN="${PLAN:-vc2-1c-2gb}"
API=https://api.vultr.com/v2
export REGION PLAN SSH_KEY_ID="${SSH_KEY_ID:-}"
auth=(-H "Authorization: Bearer ${VULTR_API_KEY}" -H "Content-Type: application/json")

OS_ID="$(curl -fsS "${auth[@]}" "$API/os?per_page=500" | python3 -c '
import json, sys
oses = json.load(sys.stdin)["os"]
print(next(o["id"] for o in oses if o["name"].startswith("Ubuntu 24.04") and o["arch"] == "x64"))')"

USER_DATA="$(printf '#!/bin/bash\nset -e\ngit clone --depth 1 %s /opt/blastproof\nbash /opt/blastproof/deploy/setup.sh > /var/log/blastproof-setup.log 2>&1\n' "$REPO_URL" | base64 | tr -d '\n')"

BODY="$(python3 -c '
import json, os, sys
body = {"region": os.environ["REGION"], "plan": os.environ["PLAN"], "os_id": int(sys.argv[1]),
        "label": "blastproof", "hostname": "blastproof", "tags": ["hackathon"], "user_data": sys.argv[2],
        "backups": "disabled"}
if os.environ.get("SSH_KEY_ID"):
    body["sshkey_id"] = [os.environ["SSH_KEY_ID"]]
print(json.dumps(body))' "$OS_ID" "$USER_DATA")"

echo "Creating $PLAN in $REGION (Ubuntu 24.04, os_id=$OS_ID)..."
RESP="$(curl -fsS "${auth[@]}" -X POST "$API/instances" -d "$BODY")"
ID="$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["instance"]["id"])')"
echo "Instance $ID created. Waiting for a public IP..."

for _ in $(seq 1 60); do
  INFO="$(curl -fsS "${auth[@]}" "$API/instances/$ID")"
  IP="$(echo "$INFO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["instance"]["main_ip"])')"
  STATUS="$(echo "$INFO" | python3 -c 'import json,sys; print(json.load(sys.stdin)["instance"]["status"])')"
  [ "$STATUS" = "active" ] && [ "$IP" != "0.0.0.0" ] && break
  sleep 10
done

echo
echo "VM is up: $IP  (setup continues in the background for ~5 minutes)"
echo "Follow it:   ssh root@$IP tail -f /var/log/blastproof-setup.log"
echo "Add the key: ssh -t root@$IP 'sudo bash /opt/blastproof/deploy/setup.sh'   # re-run is safe; it prompts for the key"
echo "App URL:     https://${IP//./-}.sslip.io"
