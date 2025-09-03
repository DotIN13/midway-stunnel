from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional
import os
import sys
import time
import getpass
import random

import keyring
from keyring.backends import fail

# -----------------------
# Constants & Defaults
# -----------------------
DEFAULT_PORT_START = 49152
DEFAULT_PORT_END = 65535
CONTROL_PERSIST = "10m"


# -----------------------
# Config dataclass
# -----------------------
@dataclass
class Config:
    app: str
    app_args: List[str]
    endpoint: str
    local_port: int
    remote_port: int
    password: Optional[str] = None
    duo_option: Optional[str] = None
    ssh_options: List[str] = field(default_factory=list)
    verbose: bool = False
    auth_timeout: int = 30
    socket_path: Path = field(init=False)
    ssh_opts: List[str] = field(init=False)
    tail: bool = False

    def __post_init__(self):
        self.socket_path = Path(f"/tmp/ssh-socket-{self.endpoint}-{time.time_ns()}")
        self.ssh_opts = ["-o", "BatchMode=no", *self.ssh_options]


# -----------------------
# Helpers
# -----------------------
def log(msg: str, cfg: Config):
    if cfg.verbose:
        print(msg)


def read_password(cfg: "Config") -> str:
    """
    Resolve an SSH password in this order:
      1) Explicit --password (cfg.password)
      2) Secure OS keyring (service + username)
      3) Interactive prompt via getpass (then saved to keyring)

    Expected (but optional) Config attributes:
      - password: Optional[str]
      - endpoint: str (required for keyring service name)
    """

    # 1) Command-line/explicit password takes precedence
    if getattr(cfg, "password", None):
        return cfg.password

    # Validate endpoint (used to namespace service in keyring)
    endpoint = getattr(cfg, "endpoint", None)
    if not endpoint:
        raise ValueError("No endpoint specified for password lookup.")

    service = f"stunnel:{endpoint}"
    username = os.environ.get("USER") or "root"

    # 2) Check keyring if a secure backend is available
    kr_backend = keyring.get_keyring()
    if not isinstance(kr_backend, fail.Keyring):
        try:
            stored = keyring.get_password(service, username)
            if stored:
                return stored
        except Exception as e:
            print(f"Warning: Unable to access the system keyring: {e}", file=sys.stderr)

    # 3) Fall back to interactive prompt
    print("No SSH password found in system keyring.")
    password = getpass.getpass("SSH password: ")

    # Try to save it into the keyring for future use
    if not isinstance(kr_backend, fail.Keyring):
        try:
            keyring.set_password(service, username, password)
            print(
                f"Password saved in system keyring for service={service}, user={username}."
            )
        except Exception as e:
            print(f"Warning: Unable to save password to keyring: {e}", file=sys.stderr)

    return password


def pick_remote_port(cfg: Config) -> int:
    if cfg.remote_port != 0:
        return cfg.remote_port
    port = random.randint(DEFAULT_PORT_START, DEFAULT_PORT_END)
    log(f"[DEBUG] Picked random remote port: {port}", cfg)
    return port


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    """
    Prompt the user for a yes/no answer. Returns True for yes, False for no.
    default=True -> [Y/n], default=False -> [y/N].
    Falls back to default on EOF or empty input.
    """
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(prompt + suffix).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    if ans in ("y", "yes"):
        return True
    if ans in ("n", "no"):
        return False
    # Unrecognized -> ask again recursively (but don’t loop forever)
    print("Please answer 'y' or 'n'.")
    return ask_yes_no(prompt, default)
