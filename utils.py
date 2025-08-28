from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional
import sys
import time
import getpass
import random

# -----------------------
# Constants & Defaults
# -----------------------
DEFAULT_PORT_START = 49152
DEFAULT_PORT_END = 65535


# -----------------------
# Config dataclass
# -----------------------
@dataclass
class Config:
    endpoint: str  # user@host or HostAlias
    local_port: int
    remote_port: int  # 0 = random ephemeral
    password: Optional[str] = None
    password_file: Optional[Path] = None
    duo_option: Optional[str] = None
    ssh_options: List[str] = field(
        default_factory=list
    )  # (kept for compatibility; ignored here)
    verbose: bool = False

    # Paramiko-specific knobs
    auth_timeout: int = 30
    banner_timeout: int = 30
    conn_timeout: int = 15
    keepalive_interval: int = 30

    # Host key policy
    strict_host_key_checking: bool = False  # True = enforce known_hosts
    known_hosts_file: Optional[Path] = None

    # Derived fields
    hostname: str = field(init=False)
    username: Optional[str] = field(init=False)

    def __post_init__(self):
        # Split endpoint "user@host" or just "host"
        if "@" in self.endpoint:
            self.username, self.hostname = self.endpoint.split("@", 1)
        else:
            self.username, self.hostname = None, self.endpoint


def log(msg: str, cfg: Config):
    if cfg.verbose:
        print(msg)


def read_password(cfg: Config) -> str:
    if cfg.password:
        return cfg.password
    if cfg.password_file:
        try:
            return cfg.password_file.read_text().splitlines()[0].strip()
        except Exception as e:
            print(f"Error reading --password-file: {e}", file=sys.stderr)
            sys.exit(2)
    return getpass.getpass("SSH password: ")


def pick_remote_port(cfg: Config) -> int:
    if cfg.remote_port != 0:
        return cfg.remote_port
    port = random.randint(DEFAULT_PORT_START, DEFAULT_PORT_END)
    log(f"[DEBUG] Picked random remote port: {port}", cfg)
    return port
