#!/usr/bin/env bash
set -euo pipefail

python -m pip install -r requirements.txt

if [[ -z "${THE_ODDS_API_KEY:-}" ]]; then
  echo "THE_ODDS_API_KEY is unavailable during setup" >&2
  exit 1
fi

umask 077
cat > .env <<EOF
DEMO_MODE=false
THE_ODDS_API_KEY=${THE_ODDS_API_KEY}
EOF
chmod 600 .env

echo "Codex setup complete: dependencies installed and local .env created without printing secrets."
