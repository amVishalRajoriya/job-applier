#!/usr/bin/env bash
# One-step start on macOS or Linux: installs what is needed the first time, then opens the dashboard.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  echo "First run: creating a virtual environment and installing dependencies (a few minutes)..."
  "$PY" -m venv .venv
fi
. .venv/bin/activate
if [ ! -f .venv/.installed ]; then
  pip install -q -r requirements.txt
  python -m playwright install chromium
  touch .venv/.installed
fi
if [ ! -f .env ]; then
  cp .env.example .env
  KEY="$(python -m jobagent.cli keygen)"
  python - "$KEY" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(".env")
p.write_text(p.read_text().replace("JOBAGENT_ENCRYPTION_KEY=\n", f"JOBAGENT_ENCRYPTION_KEY={sys.argv[1]}\n"))
PYEOF
  chmod 600 .env
  echo "Created .env with a new encryption key. Back it up: without it the saved profile cannot be read."
fi
set -a; . ./.env; set +a
python -m jobagent.cli init >/dev/null
python -m jobagent.cli dashboard --open
