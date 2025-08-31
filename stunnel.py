#!/usr/bin/env python3
"""
stunnel.py — Log in to a remote shell, start `scode`, and open an SSH tunnel.

Refactored with Config dataclass:
- All runtime state is carried in a Config object.
- Cleaner function signatures (just pass cfg).

Updates:
- Start `scode` in its own process group (setsid) to enable group-wide shutdown.
- Ensure cleanup runs while the master connection is still open (reuse it on exit).
- Redirect scode logs to ~/.stunnel/log/scode-YYYYmmdd-HHMMSS.log on the remote host.
- Tail the remote log to local stdout while the tunnel is active.
- Parse "Web UI available at http://0.0.0.0:<port>?tkn=..." from the log and print the
  local URL the user can open (http://127.0.0.1:<local_port>?tkn=...).
"""

from __future__ import annotations
import argparse
from pathlib import Path
import subprocess
import sys
import time
import shlex
from datetime import datetime
from typing import Tuple, Optional
import threading
import re
from urllib.parse import urlparse, urlunparse

from utils import Config
from utils import log, read_password, pick_remote_port
from ssh import MasterSSHConnection


# -----------------------
# CLI Parsing
# -----------------------
def parse_args() -> Config:
    p = argparse.ArgumentParser(
        description="Start remote scode and tunnel it to localhost.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("endpoint", help="SSH endpoint (user@host or Host alias)")
    p.add_argument("--local-port", type=int, default=8000, help="Local port to forward")
    p.add_argument(
        "--remote-port",
        type=int,
        default=0,
        help="Remote port to run scode on (0 = pick random ephemeral port)",
    )
    p.add_argument("--password", help="SSH password (or use --password-file / prompt)")
    p.add_argument("--password-file", type=Path, help="Path to file with SSH password")
    p.add_argument(
        "--duo-option",
        type=str,
        default=None,
        help="Duo menu selection to send (if omitted, ask interactively)",
    )
    p.add_argument(
        "--ssh-option",
        action="append",
        default=[],
        dest="ssh_options",
        help="Extra SSH option(s), repeatable (e.g., --ssh-option '-J bastion')",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose debug logging")
    args = p.parse_args()

    return Config(
        endpoint=args.endpoint,
        local_port=args.local_port,
        remote_port=args.remote_port,
        password=args.password,
        password_file=args.password_file,
        duo_option=args.duo_option,
        ssh_options=args.ssh_options,
        verbose=args.verbose,
    )


# -----------------------
# Remote Ops
# -----------------------
def run_remote(cfg: Config, cmd: str) -> str:
    ssh_cmd = ["ssh", "-S", str(cfg.socket_path), *cfg.ssh_opts, cfg.endpoint, cmd]
    log(f"[DEBUG] run_remote: {cmd}", cfg)
    result = subprocess.run(ssh_cmd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def ensure_remote_logdir(cfg: Config) -> str:
    """
    Create ~/.stunnel/log on the remote host if it doesn't exist.
    Returns the absolute path to the log directory.
    """
    cmd = "bash -lc 'mkdir -p ~/.stunnel/log && cd ~/.stunnel/log && pwd -P'"
    logdir = run_remote(cfg, cmd)
    return logdir


def start_scode(cfg: Config, port: int) -> Tuple[int, str]:
    """
    Start scode in its own session/process group so we can kill the whole tree.
    Redirect stdout/stderr to a timestamped log under ~/.stunnel/log/ on the remote host.

    Returns (pgid, log_path).
    """
    logdir = ensure_remote_logdir(cfg)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    logfile = f"{logdir}/scode-{ts}.log"

    # Keep unbuffered for Python-backed servers; harmless if not Python.
    scode_cmd = "PYTHONUNBUFFERED=1 bash -lc " + shlex.quote(
        f"setsid scode serve --local --port {port} >> {logfile} 2>&1 & echo $!"
    )

    pid_str = run_remote(cfg, scode_cmd)
    pgid = int(pid_str.strip())
    print(f"Remote scode started on port {port} (PGID: {pgid})")
    print(f"Remote log file: {logfile}")
    return pgid, logfile


def stop_scode(cfg: Config, pgid: Optional[int]):
    """
    Stop the entire process group started by start_scode.
    We send TERM to the group, wait a tick, then KILL if needed.

    Note: negative PID targets a process group: kill -TERM -<pgid>
    """
    if not pgid:
        return
    try:
        run_remote(cfg, f"kill -TERM -{pgid} || true")
        time.sleep(0.5)
        run_remote(cfg, f"kill -KILL -{pgid} || true")
        print(f"Stopped remote scode process group (PGID: {pgid})")
    except subprocess.CalledProcessError:
        print(
            f"Warning: failed to stop scode process group (PGID: {pgid}) — "
            "it may have already exited."
        )


def open_tunnel(cfg: Config, remote_port: int):
    tunnel_cmd = [
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
    subprocess.run(tunnel_cmd, check=True)
    print(
        f"Tunnel established: http://localhost:{cfg.local_port} -> "
        f"{cfg.endpoint}:127.0.0.1:{remote_port}"
    )


# -----------------------
# Log tailing (remote -> local) with URL detection
# -----------------------

_URL_RE = re.compile(
    r"(https?://)(?:0\.0\.0\.0|127\.0\.0\.1|localhost):(?P<rport>\d+)(?P<rest>[^\s]*)",
    re.IGNORECASE,
)

def _maybe_print_local_url(local_port: int, url: str, already_printed: dict):
    """
    If url looks like the 'Web UI available at ...' link, rewrite host:port to local.
    Prints only once per run.
    """
    if already_printed.get("done"):
        return

    m = _URL_RE.search(url)
    if not m:
        return

    # Parse fully to preserve path & query
    parsed = urlparse(url)
    # Replace netloc with 127.0.0.1:<local_port>
    local_netloc = f"127.0.0.1:{local_port}"
    local_url = urlunparse((
        parsed.scheme or "http",
        local_netloc,
        parsed.path or "",
        parsed.params or "",
        parsed.query or "",
        parsed.fragment or "",
    ))

    print("\n=== Web UI detected ===")
    print(f"Remote: {url}")
    print(f"Open locally: {local_url}\n")
    already_printed["done"] = True


def start_log_tail(cfg: Config, remote_log_path: str, local_port: int) -> tuple[subprocess.Popen, threading.Thread]:
    """
    Start an ssh process that tails the remote log and streams to local stdout.
    Also watches for 'Web UI available at http://0.0.0.0:<port>?tkn=...' lines and
    prints a rewritten local URL using the forwarded local_port.

    Returns (process, reader_thread).
    """
    tail_cmd = [
        "ssh",
        "-S",
        str(cfg.socket_path),
        "-n",
        *cfg.ssh_opts,
        cfg.endpoint,
        "bash",
        "-lc",
        shlex.quote(f"touch {remote_log_path} && tail -n +1 -F {remote_log_path}"),
    ]
    log("[DEBUG] starting remote tail process", cfg)

    proc = subprocess.Popen(
        tail_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered
    )

    printed_once = {"done": False}

    def _reader():
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                # Echo the log line to the local terminal
                sys.stdout.write(line)
                sys.stdout.flush()

                # Try to extract and print the local URL
                if "Web UI available at" in line or "http://" in line or "https://" in line:
                    # Extract the first URL-like token from the line
                    # This is tolerant; if multiple URLs are present, the regex picks one.
                    match = _URL_RE.search(line)
                    if match:
                        # Reconstruct full URL from the match (safer to use urlparse on the slice)
                        start, end = match.span()
                        candidate = line[start:end]
                        _maybe_print_local_url(local_port, candidate, printed_once)
        except Exception as e:
            # Don't crash the main program on tail reader errors
            print(f"[log-tail] warning: {e}", file=sys.stderr)

    t = threading.Thread(target=_reader, name="remote-log-tail", daemon=True)
    t.start()
    print(f"Tailing remote log: {remote_log_path}\n")
    return proc, t


def stop_log_tail(proc: Optional[subprocess.Popen], thread: Optional[threading.Thread]):
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
    # Threads are daemonized; no need to join strictly, but we can try briefly
    if thread is not None and thread.is_alive():
        try:
            thread.join(timeout=0.5)
        except Exception:
            pass


# -----------------------
# Main
# -----------------------
def main():
    cfg = parse_args()
    pw = read_password(cfg)
    pgid: Optional[int] = None
    tail_proc: Optional[subprocess.Popen] = None
    tail_thread: Optional[threading.Thread] = None

    print("Authenticating master SSH connection...")
    try:
        # Keep cleanup INSIDE this context so we can reuse the master connection.
        with MasterSSHConnection(cfg, pw):
            try:
                remote_port = pick_remote_port(cfg)
                pgid, log_path = start_scode(cfg, remote_port)
                open_tunnel(cfg, remote_port)

                # Start tailing the remote log to local stdout and detect the URL
                tail_proc, tail_thread = start_log_tail(cfg, log_path, cfg.local_port)

                print("Press Ctrl+C to close the master connection and exit.")
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                # Normal shutdown path
                pass
            finally:
                # Stop the tail first (so it doesn't hold the control socket busy)
                stop_log_tail(tail_proc, tail_thread)
                # Cleanup while control socket is still alive
                stop_scode(cfg, pgid)
    except subprocess.CalledProcessError as e:
        print("A command failed:", e, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print("An error occurred:", str(e), file=sys.stderr)
        sys.exit(1)
    finally:
        print("\nExiting.")


if __name__ == "__main__":
    main()
