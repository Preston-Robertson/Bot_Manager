# Discord Bot Manager

A lightweight **web dashboard** to discover, run, update, and back up Python Discord bots from one place. Designed to run headless on a Linux box (e.g. a Proxmox LXC) and be operated from any browser on your LAN.

## Features

- Auto-discovers bots inside a chosen root folder (treats the root itself as a bot if it contains an entry file)
- Detects the entry file automatically: `main.py`, `bot.py`, `run.py`, or `app.py`
- Skips the manager's own folder so it never tries to run itself
- Uses each bot's **own virtual environment** when available (`.venv`, `venv`, or `env`), checking both Windows (`Scripts/python.exe`) and POSIX (`bin/python`) layouts
- One-click **Start / Stop / Restart** per bot
- Periodic git update checks against `origin/main`
- **Manager self-update**: checks its own git checkout for updates and offers a one-click pull + restart from the dashboard
- **Auto update + restart**: pulls `main` and restarts running bots when changes are detected
- Scheduled per-bot data backups to local zip archives (gitignored)
- Per-bot backup health badge: `Healthy`, `Due Soon`, `Overdue`, or `Failed`
- Per-bot backup storage size shown in the table
- **Live, per-bot log stream** in the browser over WebSocket
- Browse and **download** backup zips directly from the dashboard
- Settings persisted to `manager_config.json`

## Requirements

- Python 3.10 or newer
- Git available in `PATH` (for update checks)
- Three pip packages: `fastapi`, `uvicorn[standard]`, `jinja2` (see [requirements.txt](requirements.txt))

Tkinter / `python3-tk` is **no longer required**.

## Installation

```bash
git clone https://github.com/<your-user>/Bot_Manager.git
cd Bot_Manager
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running

```bash
python3 app.py
```

Then open `http://<host>:28473/` in any browser on the LAN.

> **Port note.** The dashboard listens on **TCP 28473** by default. This port was chosen deliberately: it is IANA-unassigned, not used by any common service, and sits **below** Linux's default ephemeral range (`net.ipv4.ip_local_port_range` = 32768-60999), so the kernel will never auto-allocate it to an outbound socket. Override with `BOTMGR_PORT` if you need to.

On Linux / inside the LXC:
```bash
./run_manager.sh
```

On Windows (dev): double-click [run_manager.bat](run_manager.bat).

### Environment variables

| Variable        | Default     | Purpose |
|-----------------|-------------|---------|
| `BOTMGR_HOST`   | `0.0.0.0`   | Bind address. Use `127.0.0.1` to restrict to localhost. |
| `BOTMGR_PORT`   | `28473`     | TCP port. Default is intentionally obscure (see Port note above). |
| `BOTMGR_TOKEN`  | _(unset)_   | Optional shared-secret. When set, the dashboard + API + WebSocket require it. When unset, the server logs a warning and runs open (LAN-only use). |

Example (Linux):
```bash
export BOTMGR_TOKEN="$(openssl rand -hex 32)"
export BOTMGR_PORT=28473
./run_manager.sh
```

## Using the dashboard

1. **Settings** — enter the **Bots Root Folder** (an absolute path on the server).
   Optionally set a fallback **Python Executable**, the **Update Interval**, and the **Backup Interval**. Click **Save Settings**.
2. Click **Scan Bots** to (re-)discover bots under that folder.
3. Click a row to select a bot. The action bar reflects what's possible for that bot.
4. **Start / Stop / Restart / Update Now** act on the selection.
5. **Check Updates** runs a fetch + compare against `origin/main` for every git bot.
6. **Backup Now** zips the selected bot's data files; **Backup All** queues them all.
7. **Show Backup Status** opens a per-bot summary; **Browse Backups** lists archives for the selected bot and links each one for download.
8. The **Logs** panel streams every bot's stdout + system messages live over WebSocket. The last ~2000 log lines are kept in memory and replayed when a new browser opens.

## How a folder is treated as a bot

A folder qualifies as a bot if its root contains one of: `main.py`, `bot.py`, `run.py`, `app.py`.

The manager's own folder is skipped during discovery, so adding `app.py` to that list does **not** cause the manager to try to run itself.

## Python interpreter selection

When starting a bot, the manager picks the interpreter in this order:

1. The bot's own venv (`<bot>/.venv`, `<bot>/venv`, or `<bot>/env`). Both `Scripts/python.exe` and `bin/python` are checked.
2. The path entered in **Python Executable (optional)**.
3. The interpreter running the manager.

This means each bot can have its own isolated dependencies.

## Update behavior

- Updates are checked against `origin/main` using `git fetch` + `git rev-list HEAD..origin/main`.
- When **Auto update + restart** is enabled, detected updates are pulled with `git pull --ff-only` and any running bot is restarted automatically.
- Click **Update Now** for a one-off update of the selected bot.

## Manager self-update

The **Manager** card at the top of the dashboard shows the manager's own git status — branch, current commit, and whether updates exist on `origin`.

- **Check for Updates** runs `git fetch` against the manager's own remote and reports how many commits behind `origin/<branch>` you are. The periodic update check (same `update_interval_sec` used for bots) also refreshes this in the background.
- **Update Manager** runs `git pull --ff-only` on the manager directory. It refuses to run if the working tree is dirty so local edits are never silently overwritten.
- **Restart Manager** stops all running bots, saves config, then re-execs the current Python process via `os.execv`. On a systemd-managed deployment the new process replaces the old one in-place; the dashboard automatically reconnects via `/healthz` polling once the server is back.

Auto-applying a self-update is intentionally **not** offered — a bad pull would lock you out of the dashboard. The dashboard checks automatically; you decide when to pull and restart.

## Configuration

User settings are stored in `manager_config.json` (gitignored). A template is provided in [manager_config.example.json](manager_config.example.json):

```json
{
  "bots_root": "",
  "python_executable": "",
  "update_interval_sec": 120,
  "backup_interval_days": 1,
  "auto_update_restart": true
}
```

The app updates this file automatically when you save settings in the dashboard.

## Architecture

Two layers, intentionally:

- **`manager_core.py`** — the engine. Discovery, process control, git, backups, scheduling, log buffer. **Zero `tkinter` imports, zero web imports.** Could be reused by a CLI in the future.
- **`web/server.py`** — a thin FastAPI adapter that holds a single `BotManager` instance and maps HTTP/WebSocket calls to engine methods. Blocking operations (git, backups) are dispatched to a threadpool so the event loop stays responsive.

All shared state is guarded by `BotManager.bots_lock`. Logs are published via a subscriber set, so each open WebSocket gets every new line without polling.

## Security

The Tk app was implicitly local-only; a network-exposed dashboard is not. For v1:

- **Bind deliberately.** Default is `0.0.0.0:28473`. Restrict at the firewall or set `BOTMGR_HOST=127.0.0.1` if you'll reach it through SSH tunneling.
- **Set `BOTMGR_TOKEN`** for any deployment beyond a fully trusted LAN. The token is sent via `Authorization: Bearer …` on API calls and `?token=…` on the WebSocket / backup download links.
- **Path-traversal safe.** Backup-download paths are validated to stay inside `bot_data/<sanitized_bot>/`.
- **Bot stdout is visible in the log stream.** Don't print secrets from your bots, or keep the dashboard behind the token + LAN.

Out of scope (for now): TLS, multi-user accounts, RBAC. Put the dashboard behind a reverse proxy (Caddy / nginx) if you need HTTPS.

## Deploying to a Proxmox LXC (systemd)

The repo ships a ready-to-use unit file: [botmgr.service](botmgr.service) and an environment-file template: [botmgr.env.example](botmgr.env.example).

1. **Create a service user and lay out the files** (inside the container, as root):
   ```bash
   adduser --system --group --home /opt/botmgr botmgr
   git clone https://github.com/<your-user>/Bot_Manager.git /opt/botmgr
   mkdir -p /opt/bots && chown botmgr:botmgr /opt/bots
   chown -R botmgr:botmgr /opt/botmgr
   chmod +x /opt/botmgr/run_manager.sh
   ```

2. **Install Python deps** in a venv owned by the service user:
   ```bash
   sudo -u botmgr python3 -m venv /opt/botmgr/.venv
   sudo -u botmgr /opt/botmgr/.venv/bin/pip install -r /opt/botmgr/requirements.txt
   ```
   (If you go this route, also edit `run_manager.sh` to call `/opt/botmgr/.venv/bin/python` instead of system `python3`.)

3. **Set `bots_root`** in `/opt/botmgr/manager_config.json` to `/opt/bots` (or wherever you want bots cloned).

4. **Install the environment file** and generate a token:
   ```bash
   cp /opt/botmgr/botmgr.env.example /etc/botmgr.env
   sed -i "s/replace-me-.*/$(openssl rand -hex 32)/" /etc/botmgr.env
   chmod 600 /etc/botmgr.env
   chown root:botmgr /etc/botmgr.env
   ```

5. **Install and enable the unit**:
   ```bash
   cp /opt/botmgr/botmgr.service /etc/systemd/system/botmgr.service
   systemctl daemon-reload
   systemctl enable --now botmgr.service
   systemctl status botmgr.service
   journalctl -u botmgr.service -f
   ```

6. **Firewall** (example for `ufw` — adapt to `nftables` / `iptables` as you prefer). Allow only your LAN subnet:
   ```bash
   ufw default deny incoming
   ufw default allow outgoing
   ufw allow from 10.0.0.0/24 to any port 28473 proto tcp
   ufw enable
   ```
   Replace `10.0.0.0/24` with your actual LAN subnet (or a `/32` for a single admin machine).

7. **Open the dashboard** from a LAN machine: `http://<container-ip>:28473/` and paste the token from `/etc/botmgr.env` when prompted (or append `?token=...` to the URL).

## Project Structure

```
Bot_Manager/
├── app.py                       # Thin uvicorn launcher
├── manager_core.py              # Engine (no UI imports)
├── web/
│   ├── __init__.py
│   ├── server.py                # FastAPI app + WebSocket
│   ├── templates/
│   │   └── index.html
│   └── static/
│       ├── app.js
│       └── style.css
├── run_manager.sh               # Linux launcher
├── run_manager.bat              # Windows launcher
├── botmgr.service               # systemd unit for Linux/LXC deployment
├── botmgr.env.example           # template for /etc/botmgr.env
├── manager_config.example.json
├── requirements.txt
├── bot_data/                    # Local backups + backup status (gitignored)
├── .gitignore
└── README.md
```
