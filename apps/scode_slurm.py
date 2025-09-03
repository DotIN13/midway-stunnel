from __future__ import annotations
import shlex
import json
import subprocess
from typing import List, Dict, Any, Optional
from datetime import datetime

from remote_app import RemoteApp, StartedApp
from app_registry import AppRegistry
from utils import Config


class ScodeSlurmApp(RemoteApp):
    """
    Start a VSCode server via Slurm using `scode serve -- --account rcc-staff`.

    Behavior:
      - Submits a Slurm job and captures JOB_ID from stdout.
      - Polls ~/.scode/servers/<JOB_ID> for JSON with { port, token, node_ip, ... }.
      - Composes the remote URL as: http://<node_ip>:<port>/?tkn=<token>
      - Writes a framework state file including job_id + scode_state_file.
      - On stop(), runs `scancel <job_id>` and removes state file.
    """

    name = "scode-slurm"

    # We won't rely on the base-class "URL grep from log" path; we override `start`.
    # Still implement build_remote_command so the command is clear and testable.
    def build_remote_command(self, port: int, app_args: List[str], logfile: str) -> str:
        # scode ignores our "port" positional arg; we accept app_args for future flags.
        args = " ".join(shlex.quote(a) for a in app_args)
        return f"scode serve -- --account rcc-staff {args}".strip()

    def start(self, cfg: Config, port: int, app_args: List[str]) -> StartedApp:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        logdir = "$HOME/.stunnel/log"
        statedir = f"$HOME/.stunnel/servers/{self.name}"
        logfile = f"{logdir}/{self.name}-{ts}.log"
        state_file = f"{statedir}/{self.name}-{ts}"

        raw_cmd = self.build_remote_command(port, app_args, logfile)

        # One remote script:
        #   - mkdir -p
        #   - run scode serve; capture JOB_ID
        #   - poll ~/.scode/servers/$JOB_ID
        #   - build URL and write our framework state JSON (include job_id + scode_state_file)
        #   - echo the JSON so Python can parse to construct StartedApp
        script = f"""
set -euo pipefail

APP="{self.name}"
ENDPOINT="{cfg.endpoint}"
TS="{ts}"
REQ_PORT={port}
LOGDIR={logdir}
STATEDIR={statedir}
LOGFILE="{logfile}"
STATE_FILE="{state_file}"

mkdir -p "$LOGDIR" "$STATEDIR"

# Run scode serve; capture stdout/stderr into the logfile while tee'ing to a temp buffer to parse.
tmp_out="$(mktemp)"
( PYTHONUNBUFFERED=1 {raw_cmd} ) > >(tee -a "$LOGFILE" >> "$tmp_out") 2> >(tee -a "$LOGFILE" >&2)

# Extract Slurm Job ID from the buffered stdout
JOB_ID="$(grep -Eo 'Submitted batch job [0-9]+' "$tmp_out" | awk '{{print $4; exit}}' || true)"
rm -f "$tmp_out"

if [ -z "$JOB_ID" ]; then
  echo "Failed to detect Slurm Job ID from scode serve output" >&2
  exit 1
fi

SCODE_STATE_FILE="$HOME/.scode/servers/$JOB_ID"

# Poll for scode state file up to ~60s (120 * 0.5)
tries=120
while [ $tries -gt 0 ]; do
  if [ -s "$SCODE_STATE_FILE" ]; then
    # basic sanity: looks like a JSON object and has 'port' + 'token'
    if grep -q '"port"' "$SCODE_STATE_FILE" && grep -q '"token"' "$SCODE_STATE_FILE"; then
      break
    fi
  fi
  sleep 0.5
  tries=$((tries-1))
done

if [ $tries -le 0 ]; then
  echo "Timed out waiting for scode server state in $SCODE_STATE_FILE" >&2
  exit 1
fi

# Read fields from scode state JSON (avoid jq dependency; use grep/awk/sed best-effort)
NODE_IP="$(grep -E '"node_ip"\\s*:' "$SCODE_STATE_FILE" | head -n1 | sed -E 's/.*"node_ip"\\s*:\\s*"([^"]+)".*/\\1/')"
MASTER_NODE="$(grep -E '"master_node"\\s*:' "$SCODE_STATE_FILE" | head -n1 | sed -E 's/.*"master_node"\\s*:\\s*"([^"]+)".*/\\1/')"
PORT_VAL="$(grep -E '"port"\\s*:' "$SCODE_STATE_FILE" | head -n1 | sed -E 's/.*"port"\\s*:\\s*([0-9]+).*/\\1/')"
TOKEN_VAL="$(grep -E '"token"\\s*:' "$SCODE_STATE_FILE" | head -n1 | sed -E 's/.*"token"\\s*:\\s*"([^"]+)".*/\\1/')"

# Fallbacks
if [ -z "$NODE_IP" ]; then NODE_IP="127.0.0.1"; fi

# Compose remote URL the way scode web expects (tkn param)
REMOTE_URL="http://$NODE_IP:$PORT_VAL/?tkn=$TOKEN_VAL"

# Compute remote login host IP for completeness (may differ from node_ip)
HOST_IP=$(/sbin/ip route get 8.8.8.8 2>/dev/null | awk '{{print $7; exit}}' || true)
if [ -z "${{HOST_IP:-}}" ]; then HOST_IP=127.0.0.1; fi

# No long-lived foreground process to kill here; store pgid=0 and job_id separately
umask 077
cat > "$STATE_FILE" <<EOF
{{"app":"$APP",
  "host_ip":"$HOST_IP",
  "port":$PORT_VAL,
  "endpoint":"$ENDPOINT",
  "logfile":"$LOGFILE",
  "pgid":0,
  "created_at":"$TS",
  "url":"$REMOTE_URL",
  "state_file":"$STATE_FILE",
  "job_id":"$JOB_ID",
  "scode_state_file":"$SCODE_STATE_FILE",
  "node_ip":"$NODE_IP",
  "master_node":"$MASTER_NODE"}}
EOF

# Emit the JSON to stdout so Python can parse it
cat "$STATE_FILE"
""".strip()

        out = self.run_remote(cfg, "bash -lc " + shlex.quote(script))
        try:
            state: Dict[str, Any] = json.loads(out)
        except Exception as e:
            raise RuntimeError(f"Failed to parse scode-slurm state JSON:\n{out}") from e

        # Build StartedApp (pgid=0; we carry job_id in state_file JSON for stop())
        started = StartedApp(
            name=self.name,
            pgid=int(state.get("pgid", 0) or 0),
            logfile=str(state.get("logfile", "")),
            state_file=str(state.get("state_file", "")),
            remote_ip=str(state.get("node_ip", "")),
            remote_url=str(state.get("url", "")),
            remote_port=int(state.get("port", 0) or 0),
        )

        # Print a friendly hint with local rewrite
        if started.remote_url:
            local_url = self.rewrite_url(cfg.local_port, started.remote_url)
            print("\n=== VSCode (Slurm) server detected ===")
            print(f"Remote: {started.remote_url}")
            print(f"Open locally: {local_url}\n")

        return started

    def stop(self, cfg: Config, started: StartedApp) -> None:
        """
        Stop by scancel'ing the Slurm job recorded in our state JSON.
        Falls back to removing the framework state file.
        """
        if not started or not started.state_file:
            return

        # Read job_id from the state file on the remote and scancel it.
        script = f"""
set -euo pipefail
STATE_FILE="{started.state_file}"
JOB_ID=""
if [ -r "$STATE_FILE" ]; then
  JOB_ID=$(grep -E '"job_id"\\s*:' "$STATE_FILE" | head -n1 | sed -E 's/.*"job_id"\\s*:\\s*"([^"]+)".*/\\1/')
fi

if [ -n "$JOB_ID" ]; then
  scancel "$JOB_ID" 2>/dev/null || true
fi

rm -f "$STATE_FILE" 2>/dev/null || true
"""
        try:
            self.run_remote(cfg, "bash -lc " + shlex.quote(script))
            if started.pgid:
                # No-op for this app, pgid is 0; keep for symmetry/logging.
                pass
            print("Stopped scode-slurm server (via scancel if job was present).")
            print(f"Removed remote state file: {started.state_file}")
        except subprocess.CalledProcessError:
            print(
                f"Warning: cleanup may have failed for {self.name} "
                f"(state_file: {started.state_file})"
            )


# Register with your app registry
AppRegistry.register(ScodeSlurmApp)
