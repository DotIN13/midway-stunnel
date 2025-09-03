from __future__ import annotations
import subprocess
import sys
import shlex
from datetime import datetime
from typing import Optional, List, Union, Dict, Any
import threading
import re
import json
from urllib.parse import urlparse, urlunparse
from dataclasses import dataclass

from utils import Config
from utils import log as util_log

# =======================
# App framework
# =======================


@dataclass
class StartedApp:
    name: str
    pgid: int
    logfile: str
    state_file: str
    remote_ip: str
    remote_url: str
    remote_port: int


class RemoteApp:
    """
    Base framework for running a remote app through SSH with tunneling and log tailing.

    Key changes in this version:
      - start(...) now returns a JSON list (List[Dict[str, Any]]) of state objects.
        * If existing state files are present on the remote, it returns those (no new process).
        * If none are present, it starts a server, extracts a connection URL from logs,
          writes a new state file, and returns a list with that one state object.
      - tunnel(...) accepts either a single state object or a list of them. If multiple and no
        index is provided, it interactively prompts the user to choose which server to forward.
      - Each state JSON object includes "state_file" so we can manage lifecycle/actions later.
    """

    name: str = "base"
    # Default URL detector: http(s)://(0.0.0.0|127.0.0.1|localhost):<port>...
    url_regex = re.compile(
        r"(https?://)(?:0\.0\.0\.0|127\.0\.0\.1|localhost):(?P<rport>\d+)(?P<rest>[^\s]*)",
        re.IGNORECASE,
    )

    # ----- helpers -----------------------------------------------------------
    def log(self, msg: str, cfg: Config) -> None:
        util_log(msg, cfg)

    def run_remote(self, cfg: Config, cmd: str) -> str:
        ssh_cmd = ["ssh", "-S", str(cfg.socket_path), *cfg.ssh_opts, cfg.endpoint, cmd]
        self.log(f"[DEBUG] run_remote: {cmd}", cfg)
        result = subprocess.run(ssh_cmd, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    # ----- app-specific extension points ------------------------------------
    def build_remote_command(self, port: int, app_args: List[str], logfile: str) -> str:
        """Return foreground command to start the app (framework backgrounds & logs)."""
        raise NotImplementedError

    def rewrite_url(self, local_port: int, url: str) -> str:
        parsed = urlparse(url)
        local_netloc = f"127.0.0.1:{local_port}"
        return urlunparse(
            (
                parsed.scheme or "http",
                local_netloc,
                parsed.path or "",
                parsed.params or "",
                parsed.query or "",
                parsed.fragment or "",
            )
        )

    # ----- lifecycle: start / stop / tunnel / tail ---------------------------
    def start(self, cfg: Config, port: int, app_args: List[str]) -> StartedApp:
        """
        One SSH call that:
          1) Scans existing state files; if found, returns them as a JSON list (no new server).
          2) Otherwise starts a new server, scrapes a connection URL from its log, writes a state file,
             and returns a JSON list with that one state object.
        """
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        logdir = "$HOME/.stunnel/log"
        statedir = f"$HOME/.stunnel/servers/{self.name}"
        logfile = f"{logdir}/{self.name}-{ts}.log"
        state_file = f"{statedir}/{self.name}-{ts}"

        raw_cmd = self.build_remote_command(port, app_args, logfile)

        # One remote call: list states (embedding filename), or start+detect URL+write state, then echo JSON array
        script = f"""
set -euo pipefail

APP="{self.name}"
ENDPOINT="{cfg.endpoint}"
TS="{ts}"
PORT={port}
LOGDIR={logdir}
STATEDIR={statedir}
LOGFILE="{logfile}"
STATE_FILE="{state_file}"

mkdir -p "$LOGDIR" "$STATEDIR"

# Collect existing state files into a JSON array; embed filename as "state_file"
json_list="["
shopt -s nullglob
files=("$STATEDIR"/*)
if [ ${{#files[@]}} -gt 0 ]; then
  first=1
  for f in "${{files[@]}}"; do
    if [ -f "$f" ] && [ -r "$f" ]; then
      content="$(cat "$f" 2>/dev/null || true)"
      # Must look like a JSON object
      if printf '%s' "$content" | grep -q '^{{.*}}$'; then
        if [ $first -eq 0 ]; then json_list+=","
        else first=0; fi
        json_list+="$content"
      fi
    fi
  done
fi

if [ "$json_list" != "[" ]; then
  json_list+="]"
  printf '%s\\n' "$json_list"
  exit 0
fi

# No existing states — start a new server
PYTHONUNBUFFERED=1 setsid {raw_cmd} >> "$LOGFILE" 2>&1 &
PGID=$!

# Compute host IP with fallback
HOST_IP=$(/sbin/ip route get 8.8.8.8 2>/dev/null | awk '{{print $7; exit}}' || true)
if [ -z "${{HOST_IP:-}}" ]; then HOST_IP=127.0.0.1; fi

# Look for a connection URL in the log. Try up to ~15s (150 * 0.1)
pattern='https?://(0\\.0\\.0\\.0|127\\.0\\.0\\.1|localhost):[0-9]+[^[:space:]]*'
conn_url=""
for i in $(seq 1 150); do
  if grep -Eo "$pattern" "$LOGFILE" >/dev/null 2>&1; then
    conn_url=$(grep -Eo "$pattern" "$LOGFILE" | head -n1)
    break
  fi
  sleep 0.1
done

# Build the state JSON; include the URL if we found it; embed "state_file"
umask 077
if [ -n "$conn_url" ]; then
  cat > "$STATE_FILE" <<EOF
{{"app":"$APP","host_ip":"$HOST_IP","port":$PORT,"endpoint":"$ENDPOINT","logfile":"$LOGFILE","pgid":$PGID,"created_at":"$TS","url":"$conn_url","state_file":"$STATE_FILE"}}
EOF
else
  cat > "$STATE_FILE" <<EOF
{{"app":"$APP","host_ip":"$HOST_IP","port":$PORT,"endpoint":"$ENDPOINT","logfile":"$LOGFILE","pgid":$PGID,"created_at":"$TS","state_file":"$STATE_FILE"}}
EOF
fi

# Return a JSON array with this single state object
printf '[%s]\\n' "$(cat "$STATE_FILE")"
""".strip()

        remote_cmd = "bash -lc " + shlex.quote(script)
        out = self.run_remote(cfg, remote_cmd).strip()

        try:
            states = json.loads(out) if out else []
            if not isinstance(states, list):
                raise ValueError("expected a JSON list")
        except Exception as e:
            raise RuntimeError(f"Failed to parse remote states JSON:\n{out}") from e

        # Keep the full list available to callers if they want to present choices
        self.last_state_list: List[Dict[str, Any]] = states

        state = self._select_state_interactive(states)
        started = self._started_from_state(state)

        # Print the
        local_url = self.rewrite_url(cfg.local_port, started.remote_url)
        print("\n\n=== Web UI started ===")
        print(f"Remote: {started.remote_url}")
        print(f"Open locally: {local_url}\n")

        return started

    def _select_state_interactive(self, states: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Prompt user to select a state if multiple are present."""
        if not states:
            raise RuntimeError("No remote states available for selection.")
        if len(states) == 1:
            return states[0]

        # Render a simple menu
        print("\nMultiple remote servers are available. Choose one to forward:\n")
        for idx, s in enumerate(states):
            url = s.get("url", "(no url yet)")
            created = s.get("created_at", "?")
            port = s.get("port", "?")
            ep = s.get("endpoint", "?")
            print(f"[{idx}] endpoint={ep} port={port} created={created} url={url}")
        while True:
            try:
                sel = input("\nEnter selection number: ").strip()
                i = int(sel)
                if 0 <= i < len(states):
                    return states[i]
            except Exception:
                pass
            print("Invalid selection. Try again.")

    def _started_from_state(self, state: Dict[str, Any]) -> StartedApp:
        """Construct a StartedApp view from a state dict (for tailing/stop, etc.)."""
        return StartedApp(
            name=self.name,
            pgid=int(state.get("pgid", 0) or 0),
            logfile=str(state.get("logfile", "")),
            state_file=str(state.get("state_file", "")),
            remote_ip="127.0.0.1",
            remote_url=str(state.get("url", "")),
            remote_port=int(state.get("port", 0) or 0),
        )

    def tunnel(self, cfg: Config, started: StartedApp):
        """
        Establish a local->remote tunnel to the selected state.
        """
        self.log("Establishing tunnel...", cfg)
        cmd = [
            "ssh",
            "-S",
            str(cfg.socket_path),
            "-f",
            "-N",
            "-L",
            f"{cfg.local_port}:{started.remote_ip}:{started.remote_port}",
            *cfg.ssh_opts,
            cfg.endpoint,
        ]
        subprocess.run(cmd, check=True)

        print(
            f"Tunnel established: http://localhost:{cfg.local_port} -> "
            f"{cfg.endpoint}:{started.remote_ip}:{started.remote_port}"
        )

    def start_log_tail(
        self, cfg: Config, started: StartedApp, local_port: int
    ) -> tuple[subprocess.Popen, Optional[threading.Thread]]:

        tail_cmd = [
            "ssh",
            "-S",
            str(cfg.socket_path),
            "-n",
            *cfg.ssh_opts,
            cfg.endpoint,
            "bash",
            "-lc",
            shlex.quote(f"touch {started.logfile} && tail -n +1 -F {started.logfile}"),
        ]
        self.log("[DEBUG] starting remote tail process", cfg)

        proc = subprocess.Popen(tail_cmd, stdout=sys.stdout, stderr=sys.stderr)
        print(f"Tailing remote log: {started.logfile}\n")
        return proc, None

    def stop(self, cfg: Config, started: StartedApp) -> None:
        pgid = started.pgid
        state_file = started.state_file

        if not pgid or not state_file:
            return

        # One remote script handles: gentle TERM, short wait, KILL if needed, rm state file
        script = f"""
set -euo pipefail

PGID={pgid}
STATE_FILE="{state_file}"

# Helper: check if process group still exists
is_alive() {{
    kill -0 -"$PGID" 2>/dev/null
}}

# Send TERM to the process group if it looks alive
if is_alive; then
    kill -TERM -"$PGID" 2>/dev/null || true

    # Wait up to ~0.5s in small increments for clean exit
    for _ in 1 2 3 4 5; do
        if ! is_alive; then
            break
        fi
        sleep 0.1
    done

    # If still alive, send KILL
    if is_alive; then
        kill -KILL -"$PGID" 2>/dev/null || true
    fi
fi

# Remove the state file regardless
rm -f "$STATE_FILE" 2>/dev/null || true
""".strip()

        try:
            remote_cmd = "bash -lc " + shlex.quote(script)
            self.run_remote(cfg, remote_cmd)
            print(f"Stopped remote {self.name} process group (PGID: {pgid})")
            print(f"Removed remote state file: {state_file}")
        except subprocess.CalledProcessError:
            print(
                f"Warning: cleanup may have failed for {self.name} "
                f"(PGID: {pgid}, state_file: {state_file})"
            )

    def stop_log_tail(
        self, proc: Optional[subprocess.Popen], thread: Optional[threading.Thread]
    ) -> None:
        if proc is None:
            return
        # Terminate tailing process
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
        # Terminate monitoring thread
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=0.5)
            except Exception:
                pass
