#!/usr/bin/env python3
"""
midway_vscode.py — Log in to a remote shell via Paramiko, start `scode`, and open a local SSH tunnel.

- Uses Paramiko for SSH transport, auth (password + Duo keyboard-interactive), and keepalives
- Spawns a local forwarder (127.0.0.1:<local_port> → remote 127.0.0.1:<remote_port>)
"""

from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

from utils import Config, log, read_password, pick_remote_port
from ssh import ParamikoSSHConnection


# -----------------------
# CLI Parsing
# -----------------------
def parse_args() -> Config:
    p = argparse.ArgumentParser(
        description="Start remote scode and tunnel it to localhost (Paramiko).",
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
    p.add_argument("--password-file", type=str, help="Path to file with SSH password")
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
        help="(Ignored) Extra SSH option(s), kept for compatibility",
    )
    p.add_argument(
        "--strict-host-key-checking",
        action="store_true",
        help="Require known_hosts to match (no auto-add)",
    )
    p.add_argument(
        "--known-hosts-file",
        type=str,
        default=None,
        help="Path to known_hosts file (used only if strict checking is enabled)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose debug logging")
    args = p.parse_args()

    return Config(
        endpoint=args.endpoint,
        local_port=args.local_port,
        remote_port=args.remote_port,
        password=args.password,
        password_file=(
            None
            if args.password_file is None
            else Path(args.password_file)
        ),
        duo_option=args.duo_option,
        ssh_options=args.ssh_options,
        verbose=args.verbose,
        strict_host_key_checking=args.strict_host_key_checking,
        known_hosts_file=(
            None
            if args.known_hosts_file is None
            else Path(args.known_hosts_file)
        ),
    )


# -----------------------
# Remote Ops
# -----------------------
def run_remote(conn: ParamikoSSHConnection, cmd: str) -> str:
    rc, out, err = conn.run(cmd)
    if rc != 0:
        raise RuntimeError(f"Remote command failed (rc={rc}): {cmd}\n{err}")
    return out.strip()


def start_scode(conn: ParamikoSSHConnection, port: int):
    scode_cmd = f"sh -lc 'scode serve --local --port {port} >/dev/null 2>&1'"
    _stdin, _out, _err = conn.run_nonblock(scode_cmd)
    print(f"Remote scode started on port {port}.")


def open_tunnel(conn: ParamikoSSHConnection, cfg: Config, remote_port: int):
    conn.start_forwarding(cfg.local_port, "127.0.0.1", remote_port)
    print(
        f"Tunnel established: http://localhost:{cfg.local_port} -> {cfg.endpoint}:127.0.0.1:{remote_port}"
    )


# -----------------------
# Main
# -----------------------
def main():
    cfg = parse_args()
    pw = read_password(cfg)

    print("Connecting with Paramiko...")
    try:
        # Keep cleanup within the connection context so we can reuse it.
        with ParamikoSSHConnection(cfg, pw) as conn:
            try:
                remote_port = pick_remote_port(cfg)
                start_scode(conn, remote_port)
                open_tunnel(conn, cfg, remote_port)

                print("Press Ctrl+C to close the connection and exit.")
                while True:
                    time.sleep(1)

            except KeyboardInterrupt:
                # Graceful shutdown on Ctrl+C
                pass
            except Exception as e:
                # Surface other runtime errors
                print("An error occurred:", str(e), file=sys.stderr)
                sys.exit(1)
            finally:
                # Reuse the existing SSH connection for cleanup.
                print("\nExiting.")

    except Exception as e:
        # Errors before we could enter the connection context
        print("Failed to establish SSH connection:", str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
