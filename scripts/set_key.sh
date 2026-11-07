#!/usr/bin/env bash
# Store the Vultr Inference API key in .env (hidden input, never echoed), then test it.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || cp .env.example .env
read -r -s -p "Paste your Vultr Inference API key (hidden) and press Enter: " KEY; echo
[ -n "$KEY" ] || { echo "No key entered."; exit 1; }
python3 - "$KEY" <<'PY'
import re, sys, pathlib
p = pathlib.Path(".env"); key = sys.argv[1].strip()
text = p.read_text()
line = f"VULTR_INFERENCE_API_KEY={key}"
text = re.sub(r"^VULTR_INFERENCE_API_KEY=.*$", line, text, flags=re.M) if "VULTR_INFERENCE_API_KEY=" in text else text + "\n" + line + "\n"
p.write_text(text)
PY
chmod 600 .env
echo "Saved to .env. Testing the key..."
code=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  https://api.vultrinference.com/v1/chat/completions \
  -d '{"model":"deepseek-v4.1-flash-1017","messages":[{"role":"user","content":"Reply with OK"}],"max_tokens":5}')
if [ "$code" = "200" ]; then echo "Key works (HTTP 200)."; else echo "Key test returned HTTP $code - check the key or the subscription status."; fi
