#!/usr/bin/env python3
"""
midway_vscode.py — Log in to a remote shell, start `scode`, and open an SSH tunnel.

Refactored with Config dataclass:
- All runtime state is carried in a Config object.
- Cleaner function signatures (just pass cfg).
"""

from __future__ import annotations
import argparse
from pathlib import Path
import subprocess
import sys
import time

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


def start_scode(cfg: Config, port: int) -> int:
    log_path = f"~/.scode_{port}.log"
    # Use 'echo $!' to capture the background PID
    scode_cmd = f"nohup scode serve --local --port {port} > {log_path} 2>&1 & echo $!"
    pid_str = run_remote(cfg, scode_cmd)
    pid = int(pid_str.strip())
    print(f"Remote scode started on port {port} (PID: {pid}, log: {log_path})")
    return pid


def stop_scode(cfg: Config, pid: int):
    try:
        run_remote(cfg, f"pkill -TERM -P {pid}; kill -TERM {pid}")
        print(f"Stopped remote scode process (PID: {pid})")
    except subprocess.CalledProcessError:
        print(f"Warning: failed to stop scode (PID: {pid}) — it may have already exited.")


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
        f"Tunnel established: http://localhost:{cfg.local_port} -> {cfg.endpoint}:127.0.0.1:{remote_port}"
    )


# -----------------------
# Main
# -----------------------
def main():
    cfg = parse_args()
    pw = read_password(cfg)
    pid = None

    print("Authenticating master SSH connection...")
    try:
        with MasterSSHConnection(cfg, pw):
            remote_port = pick_remote_port(cfg)
            pid = start_scode(cfg, remote_port)
            open_tunnel(cfg, remote_port)

            print("Press Ctrl+C to close the master connection and exit.")
            while True:
                time.sleep(1)
    except subprocess.CalledProcessError as e:
        print("A command failed:", e, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print("An error occurred:", str(e), file=sys.stderr)
        sys.exit(1)
    finally:
        print("\nExiting.")
        # Attempt cleanup before exit
        try:
            stop_scode(cfg, pid)
        except Exception:
            pass


if __name__ == "__main__":
    main()
