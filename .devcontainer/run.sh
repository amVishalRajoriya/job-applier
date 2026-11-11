#!/usr/bin/env bash
# Starts the dashboard in a GitHub Codespace (safe to run again: it does nothing if the app already answers).
# The forwarded port is private: only your GitHub account can open it.
set -uo pipefail
cd "$(dirname "$0")/.."
export JOBAGENT_HOME="$PWD/var"
LOG="$JOBAGENT_HOME/dashboard.log"
mkdir -p "$JOBAGENT_HOME"
if curl -fs -o /dev/null http://127.0.0.1:8000/healthz; then
  echo "Job Applier is already running on port 8000."; exit 0
fi
KEYFILE="$JOBAGENT_HOME/.encryption_key"
[ -s "$KEYFILE" ] || (umask 077; python3 -m jobagent.cli keygen > "$KEYFILE")
export JOBAGENT_ENCRYPTION_KEY="$(cat "$KEYFILE")"
python3 -m jobagent.cli init >/dev/null
if [ "${1:-}" = "--foreground" ]; then
  echo "Starting Job Applier in this terminal (Ctrl+C stops it). Errors appear below."
  exec python3 -m jobagent.cli dashboard --host 0.0.0.0 --port 8000
fi
setsid nohup python3 -m jobagent.cli dashboard --host 0.0.0.0 --port 8000 >> "$LOG" 2>&1 < /dev/null &
for _ in $(seq 1 30); do
  if curl -fs -o /dev/null http://127.0.0.1:8000/healthz; then
    echo "Job Applier is running. Open the Ports tab and click the globe icon next to port 8000."; exit 0
  fi
  sleep 1
done
echo "Job Applier did not start. Last lines of $LOG:"; tail -n 30 "$LOG"; exit 1
