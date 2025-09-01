from __future__ import annotations
import shlex
from typing import List

from remote_app import RemoteApp
from app_registry import AppRegistry

# -----------------------
# Scode implementation
# -----------------------


class ScodeApp(RemoteApp):
    name = "scode"

    def build_remote_command(self, port: int, app_args: List[str], logfile: str) -> str:
        # Keep output unbuffered if scode is Python-based; safe otherwise.
        args = " ".join(shlex.quote(a) for a in app_args)
        return f"scode serve --local --port {port} {args}".strip()


# Register built-in app(s)
AppRegistry.register(ScodeApp)
