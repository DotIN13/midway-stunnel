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
CONTROL_PERSIST = "10m"

# -----------------------
# Config dataclass
# -----------------------
@dataclass
class Config:
    endpoint: str
    local_port: int
    remote_port: int
    password: Optional[str] = None
    password_file: Optional[Path] = None
    duo_option: Optional[str] = None
    ssh_options: List[str] = field(default_factory=list)
    verbose: bool = False
    auth_timeout: int = 30
    socket_path: Path = field(init=False)
    ssh_opts: List[str] = field(init=False)

    def __post_init__(self):
        self.socket_path = Path(f"/tmp/ssh-socket-{self.endpoint}-{time.time_ns()}")
        self.ssh_opts = ["-o", "BatchMode=no", *self.ssh_options]

# -----------------------
# Helpers
# -----------------------
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
