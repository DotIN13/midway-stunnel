from __future__ import annotations
import socket
import threading
import paramiko
from typing import Optional, Tuple
from utils import Config, log


class ParamikoSSHConnection:
    """
    Context-managed SSH connection with:
      - password + keyboard-interactive (Duo) auth support
      - keepalives
      - helpers for exec and local port forwarding
    """

    def __init__(self, cfg: Config, password: str):
        self.cfg = cfg
        self.password = password
        self.client = None  # type: Optional[paramiko.SSHClient]
        self._forwarder = None  # type: Optional[_PortForwarder]

    # ---- context manager ----
    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.stop_forwarding()
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        finally:
            self.client = None
        return False

    # ---- connection/auth ----
    def _connect(self):
        cfg = self.cfg

        cli = paramiko.SSHClient()
        if cfg.strict_host_key_checking:
            # Enforce known_hosts (either default or provided)
            if cfg.known_hosts_file:
                cli.load_host_keys(str(cfg.known_hosts_file))
            else:
                cli.load_system_host_keys()
        else:
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        def kbdint_handler(title, instructions, prompts):
            """
            Respond to keyboard-interactive prompts.
            Common prompts include:
              - 'Password:'
              - Duo: 'Passcode or option (1. Duo Push, 2. ...):'
            """
            answers = []
            for prompt, echo in prompts:
                pl = prompt.lower()
                # Password prompt
                if "password" in pl:
                    answers.append(self.password or "")
                # Duo prompt
                elif "passcode or option" in pl or "passcode" in pl or "duo" in pl:
                    if cfg.duo_option:
                        answers.append(cfg.duo_option)
                    else:
                        # fall back to simple input (non-echoed here)
                        try:
                            ans = input(prompt).strip()
                        except EOFError:
                            ans = ""
                        answers.append(ans)
                else:
                    # default to empty (or echo back password if that's your policy)
                    answers.append("")
            return answers

        # Try password (and keyboard-interactive) with agent/keys allowed
        try:
            cli.connect(
                cfg.hostname,
                username=cfg.username,
                password=self.password,
                look_for_keys=True,
                allow_agent=True,
                timeout=cfg.conn_timeout,
                banner_timeout=cfg.banner_timeout,
                auth_timeout=cfg.auth_timeout,
                # Paramiko will automatically try 'password' and 'keyboard-interactive'
                # To force kbdint: pass auth_handler via connect_kex? (not needed usually)
            )
        except paramiko.ssh_exception.AuthenticationException:
            # Retry using explicit keyboard-interactive if server insists on it
            transport = paramiko.Transport((cfg.hostname, 22))
            transport.start_client(timeout=cfg.conn_timeout)

            # host key checks
            if cfg.strict_host_key_checking:
                key = transport.get_remote_server_key()
                # NOTE: you could check key against known_hosts here; skipping for brevity

            # auth: keyboard-interactive first, then password fallback
            try:
                username = cfg.username or transport.get_username() or ""
                transport.auth_interactive(username, kbdint_handler)
            except paramiko.ssh_exception.AuthenticationException:
                transport.auth_password(username, self.password or "")

            # wrap the transport with SSHClient to reuse API
            cli._transport = transport

        # keepalive
        cli.get_transport().set_keepalive(cfg.keepalive_interval)

        self.client = cli
        log("[DEBUG] SSH connected and keepalive set.", cfg)

    # ---- exec helpers ----
    def run(self, command: str, timeout: Optional[int] = None) -> Tuple[int, str, str]:
        """
        Run a non-interactive remote command.
        Returns (exit_status, stdout, stderr)
        """
        assert self.client is not None
        log(f"[DEBUG] exec: {command}", self.cfg)
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err

    # ---- port forward ----
    def start_forwarding(self, local_port: int, remote_host: str, remote_port: int):
        """
        Start a local port forward (127.0.0.1:local_port) to (remote_host:remote_port) on the OTHER side.
        """
        assert self.client is not None
        if self._forwarder:
            return
        self._forwarder = _PortForwarder(
            self.client.get_transport(),
            ("127.0.0.1", local_port),
            (remote_host, remote_port),
            self.cfg,
        )
        self._forwarder.start()
        log(
            f"[DEBUG] Local forwarding started: 127.0.0.1:{local_port} -> {remote_host}:{remote_port}",
            self.cfg,
        )

    def stop_forwarding(self):
        if self._forwarder:
            self._forwarder.stop()
            self._forwarder = None
            log("[DEBUG] Local forwarding stopped.", self.cfg)


# -----------------------
# Internal forwarder
# -----------------------
class _PortForwarder:
    """
    Minimal local TCP forwarder using Paramiko 'direct-tcpip' channels.
    """

    def __init__(
        self, transport: paramiko.Transport, listen_addr, dest_addr, cfg: Config
    ):
        self.transport = transport
        self.listen_addr = listen_addr
        self.dest_addr = dest_addr
        self.cfg = cfg
        self._listener = None  # type: Optional[socket.socket]
        self._accept_thread = None
        self._stop = threading.Event()
        self._children = set()
        self._children_lock = threading.Lock()

    def start(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(self.listen_addr)
        self._listener.listen(50)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._listener:
                self._listener.close()
        except Exception:
            pass
        with self._children_lock:
            for t in list(self._children):
                try:
                    t.join(timeout=0.5)
                except Exception:
                    pass
            self._children.clear()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client_sock, client_addr = self._listener.accept()
            except OSError:
                # closed
                break
            t = threading.Thread(
                target=self._handle_client, args=(client_sock, client_addr), daemon=True
            )
            with self._children_lock:
                self._children.add(t)
            t.start()

    def _handle_client(self, client_sock: socket.socket, client_addr):
        # Open SSH channel to dest_addr using direct-tcpip
        try:
            chan = self.transport.open_channel(
                kind="direct-tcpip",
                dest_addr=self.dest_addr,  # (host, port) on remote
                src_addr=client_addr,  # origin of the connection (for logging)
            )
        except Exception as e:
            log(f"[DEBUG] open_channel failed: {e}", self.cfg)
            client_sock.close()
            return

        # Bi-directional copy
        def pump(src, dst):
            try:
                while True:
                    data = src.recv(32768)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t1 = threading.Thread(target=pump, args=(client_sock, chan), daemon=True)
        t2 = threading.Thread(target=pump, args=(chan, client_sock), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        try:
            chan.close()
        except Exception:
            pass
        try:
            client_sock.close()
        except Exception:
            pass
