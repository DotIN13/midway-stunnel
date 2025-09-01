#!/usr/bin/env python3
"""
midway_vscode.py — Log in to a remote shell, start `scode`, and open an SSH tunnel.

Refactored with Config dataclass:
- All runtime state is carried in a Config object.
- Cleaner function signatures (just pass cfg).
"""

from __future__ import annotations
import subprocess
import sys

from utils import Config
from utils import log

# -----------------------
# Constants & Defaults
# -----------------------
DEFAULT_PORT_START = 49152
DEFAULT_PORT_END = 65535
CONTROL_PERSIST = "10m"


# -----------------------
# Master SSH Connection
# -----------------------
class MasterSSHConnection:
    def __init__(self, cfg: Config, password: str):
        self.cfg = cfg
        self.password = password

    def __enter__(self):
        self._authenticate_with_pexpect()
        return self

    def __exit__(self, exc_type, exc, tb):
        close_cmd = [
            "ssh",
            "-S",
            str(self.cfg.socket_path),
            "-O",
            "exit",
            *self.cfg.ssh_opts,
            self.cfg.endpoint,
        ]
        subprocess.run(close_cmd, check=False)
        try:
            if self.cfg.socket_path.exists():
                self.cfg.socket_path.unlink()
        except Exception:
            pass
        return False

    def _authenticate_with_pexpect(self):
        try:
            import pexpect
        except ImportError:
            print(
                "The 'pexpect' module is required. Install with: pip install pexpect",
                file=sys.stderr,
            )
            sys.exit(3)

        cmd = (
            [
                "ssh",
                "-M",
                "-S",
                str(self.cfg.socket_path),
                "-o",
                f"ControlPersist={CONTROL_PERSIST}",
            ]
            + self.cfg.ssh_opts
            + [self.cfg.endpoint, 'echo "Master connection ready"']
        )

        # Prompts we may encounter
        hostkey_prompt = r"Are you sure you want to continue connecting.*\?\s*$"
        password_prompt = r"[Pp]assword:\s*$"
        duo_prompt = r"Passcode or option .*:.*$"
        success_line = r"Master connection ready"

        child = pexpect.spawn(
            cmd[0], cmd[1:], encoding="utf-8", timeout=self.cfg.auth_timeout
        )

        try:
            while True:
                idx = child.expect(
                    [
                        hostkey_prompt,  # 0 — first-time host key acceptance
                        password_prompt,  # 1
                        duo_prompt,  # 2
                        success_line,  # 3
                        pexpect.EOF,  # 4
                        pexpect.TIMEOUT,  # 5
                    ]
                )

                print(child.before, end="")
                print(child.after, end="")
                log(f"[DEBUG] pexpect matched index: {idx}", self.cfg)

                if idx == 0:
                    # Let the user explicitly type the response (typically 'yes')
                    try:
                        resp = input(
                            "Type 'yes' to add host to known_hosts (or 'no' to abort): "
                        ).strip()
                    except EOFError:
                        resp = "no"
                    child.sendline(resp)

                    # If they typed 'no', we’ll likely hit EOF soon; loop will handle it.
                    continue

                if idx == 1:
                    child.sendline(self.password)
                    continue

                if idx == 2:
                    if self.cfg.duo_option:
                        log(
                            f"[DEBUG] Sending Duo option: {self.cfg.duo_option}",
                            self.cfg,
                        )
                        child.sendline(self.cfg.duo_option)
                    else:
                        user_choice = input("Enter Duo option: ").strip()
                        child.sendline(user_choice)
                    continue

                if idx == 3:
                    # success line printed on the remote, connection is authenticated
                    try:
                        child.expect(pexpect.EOF, timeout=2)
                    except Exception:
                        pass
                    break

                if idx == 4:
                    # EOF before success; exit loop and let caller handle
                    break

                if idx == 5:
                    print("SSH authentication timed out.", file=sys.stderr)
                    sys.exit(1)
        finally:
            try:
                child.close()
            except Exception:
                pass
