# STunnel: One-command remote web apps on Midway

Run remote, browser-based apps (like VS Code Web) **on the Midway cluster** directly from your local terminal. `stunnel` handles:

* One reusable **master SSH** connection (ControlMaster)
* App **startup** on login or compute nodes
* Local ⇄ login/compute node **port forwarding** (auto-tunnel)
* Optional **live log tailing** (`--tail`)
* Robust **state files** on the remote host
* Friendly **shutdown** prompt (or `scancel` for Slurm)

It ships with two “apps”:

* `scode-local` — `scode serve --local` on the login node
* `scode-slurm` — `scode serve` via **Slurm**, then polls the scode state file and exposes the VS Code Web URL

---

## Table of contents

- [STunnel: One-command remote web apps on Midway](#stunnel-one-command-remote-web-apps-on-midway)
  - [Table of contents](#table-of-contents)
  - [Why this exists](#why-this-exists)
  - [Requirements](#requirements)
  - [Installation](#installation)
  - [Quickstart](#quickstart)
    - [VS Code on the login node](#vs-code-on-the-login-node)
    - [VS Code via Slurm on a compute node](#vs-code-via-slurm-on-a-compute-node)
  - [Usage](#usage)
    - [Common flags](#common-flags)
    - [VS Code on login node (`scode-local`)](#vs-code-on-login-node-scode-local)
    - [VS Code via Slurm (`scode-slurm`)](#vs-code-via-slurm-scode-slurm)
  - [Tunneling model](#tunneling-model)
  - [State files \& lifecycle](#state-files--lifecycle)
  - [Extending with new apps](#extending-with-new-apps)
  - [Troubleshooting](#troubleshooting)
  - [Security notes](#security-notes)
  - [License](#license)

---

## Why this exists

Midway is powerful but has moving parts: SSH options, Slurm queues, app logs, and port forwarding. `stunnel` wraps all of that behind a single CLI so you can run VS Code Web (and other web UIs) on Midway with a local URL like `http://127.0.0.1:8000`.

---

## Requirements

* **Python 3.9+**
* **OpenSSH client** on your local machine
* Works with **Linux, macOS, WSL (Windows Subsystem for Linux)** (Windows Powershell support is coming soon)
* An RCC **Midway** account and working SSH setup (keys or password; Duo as needed)
* `scode` installed and activated on Midway (e.g., `module load scode` in your `~/.bashrc` or similar)

---

## Installation

```bash
git clone https://github.com/dotin13/midway-stunnel.git
cd midway-stunnel
# (optional) create a venv
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Place the repo on your PATH or run with `python3 stunnel.py`.

---

## Quickstart

### VS Code on the login node

```bash
./stunnel.py <username>@midway3-login3.rcc.uchicago.edu --app scode-local
```

* Opens an SSH control connection
* Starts `scode serve --local --port <remote-port>`
* Forwards `http://127.0.0.1:8000` → Midway login host → `127.0.0.1:<remote-port>`
* Prints a ready-to-open local URL

### VS Code via Slurm on a compute node

```bash
./stunnel.py midway3-login3.rcc.uchicago.edu --app scode-slurm
```

* Runs `scode serve -- --account rcc-staff`
* Reads the **Slurm Job ID**, then polls `~/.scode/servers/<JOBID>` for `{ node_ip, port, token }`
* Creates a remote state file with all metadata
* Forwards `http://127.0.0.1:8000` → login host → `node_ip:port`
* Prints the local URL

> On exit, you’ll be asked: **“Stop the remote server now? \[Y/n]”**
>
> * `scode-local`: kills the server process group
> * `scode-slurm`: runs `scancel <JOBID>`

---

## Usage

```bash
./stunnel.py <endpoint> [flags]
```

### Common flags

* `endpoint` – SSH target (e.g., `user@midway3-login3.rcc.uchicago.edu` or an SSH config Host alias)
* `--app {scode-local,scode-slurm}` – which app to run (default: `scode`)
* `--app-arg ...` – repeatable; passed verbatim to the app
* `--local-port 8000` – local listen port (default: 8000)
* `--remote-port 0` – remote port hint (some apps may ignore; `0` = random)
* `--ssh-option '-J bastion'` – repeatable; extra SSH flags for jumps/proxies
* `--duo-option ...` – optional Duo selection string (if your setup uses it)
* `--verbose` – extra debug logs
* `--tail` – live log tailing (off by default)

### VS Code on login node (`scode-local`)

```bash
./stunnel.py midway3-login3 --app scode-local --local-port 8080
```

Opens: `http://127.0.0.1:8080`

### VS Code via Slurm (`scode-slurm`)

```bash
./stunnel.py midway3-login3 --app scode-slurm
```

* Submission output is logged
* The tool polls `~/.scode/servers/<JOBID>` until it sees `port` and `token`
* Remote URL shape: `http://<node_ip>:<port>/?tkn=<token>`
* Local rewrite is printed for convenience

> If the compute node isn’t reachable from the login node by IP (firewall/router), add the proper jump hosts via `--ssh-option` or your `~/.ssh/config`.

---

## Tunneling model

`stunnel` sets up:

```bash
Local: http://127.0.0.1:<LOCAL_PORT>
  ⇅ SSH -L <LOCAL_PORT>:<REMOTE_IP>:<REMOTE_PORT>
Midway login: forwards to compute/login host
  ⇢ App’s HTTP server on <REMOTE_IP>:<REMOTE_PORT>
```

* `scode-local`: `REMOTE_IP=127.0.0.1` (on the login host)
* `scode-slurm`: `REMOTE_IP=<node_ip>` from the scode server file

SSH options (`-J`, `ProxyJump`, etc.) come from `--ssh-option` and your SSH config.

---

## State files & lifecycle

On the **remote** host:

* Logs: `~/.stunnel/log/<app>-YYYYmmdd-HHMMSS.log`
* State: `~/.stunnel/servers/<app>/<app>-YYYYmmdd-HHMMSS`

Each state file is a single JSON object, e.g.:

```json
{
  "app": "scode-slurm",
  "endpoint": "user@midway3-login3",
  "created_at": "20250903-014210",
  "logfile": "/home/user/.stunnel/log/scode-slurm-20250903-014210.log",
  "host_ip": "10.50.250.11",
  "node_ip": "10.50.250.11",
  "port": 61044,
  "url": "http://10.50.250.11:61044/?tkn=XXXXXXXX",
  "state_file": "/home/user/.stunnel/servers/scode-slurm/scode-slurm-20250903-014210",
  "job_id": "36383089",
  "pgid": 0
}
```

At startup:

* If prior state files exist, `stunnel` lists them and asks which one to forward.
* Otherwise it starts a fresh server and writes a new state file.

On shutdown:

* You’re asked whether to stop the remote server.
* `scode-local`: kills the app process group and removes the state file.
* `scode-slurm`: `scancel <job_id>`, then removes the state file.

---

## Extending with new apps

Add a subclass of `RemoteApp` and register it:

```python
# apps/my_app.py
from remote_app import RemoteApp
from app_registry import AppRegistry
import shlex
from typing import List

class MyWebApp(RemoteApp):
    name = "my-webapp"

    def build_remote_command(self, port: int, app_args: List[str], logfile: str) -> str:
        args = " ".join(shlex.quote(a) for a in app_args)
        return f"my-webapp --port {port} {args}"

AppRegistry.register(MyWebApp)
```

If your app writes its own state somewhere (like scode on Slurm), override `start()` to:

* launch the job,
* poll its state file or readiness probe,
* synthesize a `remote_url` and `remote_port`,
* write our framework state JSON,
* return a `StartedApp`.

---

## Troubleshooting

* **Tunnel works but the page doesn’t load**

  * Verify the **compute node IP** is reachable from the login node. Adjust SSH `-J`/ProxyJump if necessary.
  * Firewalls/security groups may block arbitrary ports; check with RCC docs.

* **Slurm job pending for a long time**

  * Check partition/account limits. Try `scode jobs status <JOBID>`.

* **“Failed to parse remote states JSON”**

  * Inspect remote log: `~/.stunnel/log/<app>-*.log`.
  * State files live in `~/.stunnel/servers/<app>/` — ensure they’re valid JSON.

* **Duo/interactive auth issues**

  * Try adding `--ssh-option '-oKbdInteractiveAuthentication=yes'` or match your local SSH config.
  * If using a bastion: `--ssh-option '-J bastion.example.edu'`.

---

## Security notes

* Tokens in URLs (e.g., VS Code `?tkn=`) are sensitive. Treat state files and logs as **secrets**.
* Use SSH keys where possible; prefer agent forwarding over plaintext passwords.
* Clean up: choose **Yes** at the stop prompt unless you intend to leave the service running.
* Consider per-user permissions on `~/.stunnel` (the tool uses restrictive `umask` when writing state).

---

## License

MIT.
