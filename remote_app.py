from __future__ import annotations
import subprocess
import sys
import shlex
from datetime import datetime
from typing import Optional, List
import threading
import re
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
    remote_port: int


class RemoteApp:
    """
    Base framework for running a remote app through SSH with tunneling and log tailing.
    Subclasses override:
      - name (short id)
      - build_remote_command(port: int, app_args: List[str], logfile: str) -> str
      - optionally: url_regex, rewrite_url(), wants_url_hinting()
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

    def ensure_remote_dirs(self, cfg: Config) -> tuple[str, str]:
        """
        Ensure ~/.stunnel/log and ~/.stunnel/<appname> exist on the remote host.
        Returns (logdir, statedir).
        """
        logdir = "$HOME/.stunnel/log"
        statedir = f"$HOME/.stunnel/servers/{self.name}"
        cmd = "bash -lc 'mkdir -p {logdir} {statedir}'".format(
            logdir=shlex.quote(logdir), statedir=shlex.quote(statedir)
        )
        self.run_remote(cfg, cmd)
        return logdir, statedir

    def write_server_state(
        self, cfg: Config, state_file: str, port: int, logfile: str, pgid: int, ts: str
    ) -> str:
        """
        Write a JSON state file on the REMOTE host in one shot (compute IP there too).
        Creates the directory, expands ~, and writes JSON.
        """

        script = f"""
set -e

APP="{self.name}"
ENDPOINT="{cfg.endpoint}"
LOGFILE="{logfile}"
TS="{ts}"
PORT={port}
PGID={pgid}
STATE_FILE="{state_file}"

# Compute remote host IP (fall back to 127.0.0.1 if not found)
HOST_IP=$(/sbin/ip route get 8.8.8.8 2>/dev/null | awk '{{print $7; exit}}')

# Write JSON atomically-ish
umask 077
cat > "$STATE_FILE" <<EOF
{{"app":"$APP","host_ip":"$HOST_IP","port":$PORT,"endpoint":"$ENDPOINT","logfile":"$LOGFILE","pgid":$PGID,"created_at":"$TS"}}
EOF
""".strip()

        cmd = "bash -lc " + shlex.quote(script)
        self.run_remote(cfg, cmd).strip()
        print(f"Remote state file: {state_file}")
        return state_file

    # ----- app-specific extension points ------------------------------------
    def build_remote_command(self, port: int, app_args: List[str], logfile: str) -> str:
        """Return foreground command to start the app (framework backgrounds & logs)."""
        raise NotImplementedError

    def wants_url_hinting(self) -> bool:
        return True

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
        logdir, statedir = self.ensure_remote_dirs(cfg)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        logfile = f"{logdir}/{self.name}-{ts}.log"
        state_file = f"{statedir}/{self.name}-{ts}"

        raw_cmd = self.build_remote_command(port, app_args, logfile)
        remote_cmd = "bash -lc " + shlex.quote(
            f"PYTHONUNBUFFERED=1 setsid {raw_cmd} >> {logfile} 2>&1 & echo $!"
        )
        pid_str = self.run_remote(cfg, remote_cmd)
        pgid = int(pid_str.strip())

        print(f"Remote {self.name} started on port {port} (PGID: {pgid})")
        print(f"Remote log file: {logfile}")

        # Save server state JSON on the remote
        try:
            state_file = self.write_server_state(
                cfg, state_file=state_file, port=port, logfile=logfile, pgid=pgid, ts=ts
            )
        except Exception as e:
            print("Error saving server state file: ", str(e), file=sys.stderr)

        return StartedApp(
            name=self.name,
            pgid=pgid,
            logfile=logfile,
            state_file=state_file,
            remote_port=port,
        )

    def tunnel(self, cfg: Config, remote_port: int) -> None:
        self.log("Establishing tunnel...", cfg)
        cmd = [
            "ssh",
            "-S",
            str(cfg.socket_path),
            "-f",
            "-N",
            "-L",
            f"{cfg.local_port}:127.0.0.1:{remote_port}",
            *cfg.ssh_opts,
            cfg.endpoint,
        ]
        subprocess.run(cmd, check=True)
        print(
            f"Tunnel established: http://localhost:{cfg.local_port} -> "
            f"{cfg.endpoint}:127.0.0.1:{remote_port}"
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

        if not self.wants_url_hinting():
            proc = subprocess.Popen(tail_cmd, stdout=sys.stdout, stderr=sys.stderr)
            print(f"Tailing remote log: {started.logfile}\n")
            return proc, None

        proc = subprocess.Popen(
            tail_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        printed_once = {"done": False}
        url_re = getattr(self, "url_regex", None)

        def _reader():
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()

                    if printed_once.get("done") or not url_re:
                        continue

                    if (
                        "http://" in line
                        or "https://" in line
                        or "Web UI available" in line
                    ):
                        m = url_re.search(line)
                        if m:
                            s, e = m.span()
                            remote_url = line[s:e]
                            local_url = self.rewrite_url(local_port, remote_url)
                            print("\n=== Web UI detected ===")
                            print(f"Remote: {remote_url}")
                            print(f"Open locally: {local_url}\n")
                            printed_once["done"] = True
            except Exception as e:
                print(f"[log-tail] warning: {e}", file=sys.stderr)

        t = threading.Thread(target=_reader, name=f"{self.name}-log-tail", daemon=True)
        t.start()
        print(f"Tailing remote log: {started.logfile}\n")
        return proc, t

    def stop(self, cfg: Config, started: StartedApp) -> None:
        pgid = started.pgid
        state_file = started.state_file

        if not pgid or not state_file:
            return

        try:
            # Build a compound shell command:
            # - TERM the process group
            # - sleep 0.5s
            # - KILL the process group
            # - rm the state file (ignores missing file)
            # Wrap each part with `|| true` to avoid aborting on errors
            cmd_parts = []
            cmd_parts.append(f"kill -TERM -{pgid} || true")
            cmd_parts.append("sleep 0.5")
            cmd_parts.append(f"kill -KILL -{pgid} || true")
            cmd_parts.append(f'rm -f "{state_file}" || true')

            remote_cmd = "bash -lc " + shlex.quote(" && ".join(cmd_parts))
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
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=0.5)
            except Exception:
                pass
