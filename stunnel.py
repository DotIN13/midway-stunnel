#!/usr/bin/env python3
"""
stunnel.py — Log in to a remote shell, start an app (e.g., `scode`), and open an SSH tunnel.

Framework:
- RemoteApp base class owns lifecycle: start, tunnel, start_log_tail, stop_log_tail, log.
- ScodeApp overrides app-specific pieces (command build, URL detection/rewrite).
- Master SSH control socket reused for all remote ops; cleanup while it's alive.
- App runs in its own process group (setsid) for group-wide shutdown.
- Logs go to ~/.stunnel/log/<app>-YYYYmmdd-HHMMSS.log (remote).
- Optionally live-tail logs locally if --tail is provided.
"""

from __future__ import annotations
import argparse
from pathlib import Path
import subprocess
import sys
import time
from typing import Optional
import threading

from utils import Config
from utils import read_password, pick_remote_port, ask_yes_no
from ssh import MasterSSHConnection
from app_registry import AppRegistry
from remote_app import StartedApp

import apps.scode_local
import apps.scode_slurm


# =======================
# CLI
# =======================


def parse_args() -> Config:
    p = argparse.ArgumentParser(
        description="Start a remote app (e.g., scode) and tunnel it to localhost.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("endpoint", help="SSH endpoint (user@host or Host alias)")
    p.add_argument(
        "--app", default="scode", choices=AppRegistry.choices(), help="Which app to run"
    )
    p.add_argument(
        "--app-arg",
        action="append",
        default=[],
        dest="app_args",
        help="Extra app argument(s), repeatable (passed verbatim to the app)",
    )
    p.add_argument("--local-port", type=int, default=8000, help="Local port to forward")
    p.add_argument(
        "--remote-port",
        type=int,
        default=0,
        help="Remote port to run the app on (0=random)",
    )
    p.add_argument("--password", help="SSH password (or use --password-file / prompt)")
    p.add_argument("--password-file", type=Path, help="Path to file with SSH password")
    p.add_argument(
        "--duo-option", type=str, default=None, help="Duo menu selection to send"
    )
    p.add_argument(
        "--ssh-option",
        action="append",
        default=[],
        dest="ssh_options",
        help="Extra SSH option(s), repeatable (e.g., --ssh-option '-J bastion')",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose debug logging")
    p.add_argument(
        "--tail",
        action="store_true",
        help="Enable live log tailing (disabled by default)",
    )

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
        app=args.app,
        app_args=args.app_args,
        tail=args.tail,
    )


# =======================
# Main
# =======================


def main():
    cfg = parse_args()
    pw = read_password(cfg)

    AppClass = AppRegistry.get(getattr(cfg, "app", "scode-local"))
    app = AppClass()

    print(f"Starting app: {app.name}...")
    print("Authenticating master SSH connection...")

    tail_proc: Optional[subprocess.Popen] = None
    tail_thread: Optional[threading.Thread] = None
    started: Optional[StartedApp] = None

    try:
        with MasterSSHConnection(cfg, pw):
            try:
                remote_port = cfg.remote_port or pick_remote_port(cfg)
                started = app.start(cfg, remote_port, getattr(cfg, "app_args", []))

                # If multiple states are returned, user selection happens inside tunnel()
                started_app = app.tunnel(cfg, started)

                if getattr(cfg, "tail", False):
                    tail_proc, tail_thread = app.start_log_tail(
                        cfg, started_app, cfg.local_port
                    )

                print("Press Ctrl+C to close the master connection and exit.")
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            except Exception as e:
                print(
                    "An error occurred while starting the application: ",
                    str(e),
                    file=sys.stderr,
                )
                raise e
            finally:
                if getattr(cfg, "tail", False):
                    app.stop_log_tail(tail_proc, tail_thread)

                # Prompt user whether to stop the remote server
                if started is not None:
                    try:
                        if ask_yes_no("Stop the remote server now?", default=True):
                            app.stop(cfg, started)
                        else:
                            print("Leaving the remote server running.")
                    except Exception as e:
                        print(
                            f"Warning: Failed to stop remote server: {e}",
                            file=sys.stderr,
                        )
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
