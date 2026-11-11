#!/usr/bin/env bash
# Starts the dashboard in a GitHub Codespace. The forwarded port is private: only your GitHub account can open it.
set -euo pipefail
cd "$(dirname "$0")/.."
export JOBAGENT_HOME="${JOBAGENT_HOME:-$PWD/var}"
mkdir -p "$JOBAGENT_HOME"
KEYFILE="$JOBAGENT_HOME/.encryption_key"
[ -f "$KEYFILE" ] || (umask 077; python3 -m jobagent.cli keygen > "$KEYFILE")
export JOBAGENT_ENCRYPTION_KEY="$(cat "$KEYFILE")"
python3 -m jobagent.cli init >/dev/null
pkill -f "jobagent.cli dashboard" 2>/dev/null || true
setsid nohup python3 -m jobagent.cli dashboard --host 0.0.0.0 --port 8000 > "$JOBAGENT_HOME/dashboard.log" 2>&1 &
echo "Job Applier is starting on port 8000 (log: $JOBAGENT_HOME/dashboard.log)"
