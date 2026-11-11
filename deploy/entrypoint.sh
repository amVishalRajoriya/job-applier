#!/usr/bin/env bash
# Container entry point for hosted use (Hugging Face Spaces, Render, any Docker host).
set -euo pipefail
export JOBAGENT_HOME="${JOBAGENT_HOME:-/data}"
export JOBAGENT_HOSTED=1
mkdir -p "$JOBAGENT_HOME"
if [ -z "${JOBAGENT_DASHBOARD_TOKEN:-}" ]; then
  echo "Refusing to start: set the JOBAGENT_DASHBOARD_TOKEN secret on your host. It is the password that" >&2
  echo "keeps your resume and details private on a public URL." >&2
  exit 1
fi
if [ -z "${JOBAGENT_ENCRYPTION_KEY:-}" ]; then
  # No key given: keep one next to the data. It lives exactly as long as the data it protects.
  KEYFILE="$JOBAGENT_HOME/.encryption_key"
  [ -f "$KEYFILE" ] || (umask 077; python3 -m jobagent.cli keygen > "$KEYFILE")
  export JOBAGENT_ENCRYPTION_KEY="$(cat "$KEYFILE")"
fi
python3 -m jobagent.cli init >/dev/null
exec python3 -m jobagent.cli dashboard --host 0.0.0.0 --port "${PORT:-7860}"
