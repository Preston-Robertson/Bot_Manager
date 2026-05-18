# Discord Bot Manager

A lightweight desktop GUI to discover, run, update, and restart Python Discord bots from one place — built so you can manage every bot on your home server without juggling terminals.

## Features

- Auto-discovers bots inside a chosen root folder (treats the root itself as a bot if it contains an entry file)
- Detects the entry file automatically: `main.py`, `bot.py`, `run.py`, or `app.py`
- Uses each bot's **own virtual environment** when available (`.venv`, `venv`, or `env`)
- One-click **Start Bot / Stop Bot** toggle plus **Restart**
- Periodic git update checks against `origin/main`
- **Auto update + restart**: pulls `main` and restarts running bots when changes are detected
- Scheduled per-bot data backups into local zip archives (stored in a gitignored folder)
- Backup status menu to view the last successful backup time per bot
- Per-bot backup health badge: `Healthy`, `Due Soon`, `Overdue`, or `Failed`
- Per-bot backup storage usage shown in the bot table
- Live, per-bot log stream in the GUI
- Settings persisted to `manager_config.json`

## Requirements

- Python 3.10 or newer
- Git available in `PATH` (for update checks)
- Tkinter (bundled with standard Python on Windows / macOS; on Linux install `python3-tk`)

No third-party Python packages are required — the app uses the standard library only.

## Installation

```powershell
git clone https://github.com/<your-user>/Bot_Manager.git
cd Bot_Manager
```

## Usage

Start the app:

```powershell
python app.py
```

Or, on Windows, double-click `run_manager.bat`.

In the GUI:

1. Set **Bots Root Folder** to either:
   - a parent folder containing several bot folders, or
   - a single bot folder itself.
2. Click **Scan Bots**.
3. Select a bot, then use **Start Bot / Stop Bot**, **Restart**, **Update Now**, or **Check Updates**.
4. Watch each bot's **Backup** health badge and **Backup Size** directly in the table.

Backups:

1. Set **Backup Interval (days)** to choose how often backups run.
2. Use **Backup Now** for immediate backups from the main action bar.
3. Use the **Backups** menu to see last backup times, back up selected bot now, or back up all bots now.
4. Use **Open Backup Folder** (button or Backups menu) to open local backup storage directly in your file explorer.

Backups are saved under `bot_data/<bot_name>/` as timestamped `.zip` files and are kept until you manually delete them.

### How a folder is treated as a bot

A folder qualifies as a bot if its root contains one of:
`main.py`, `bot.py`, `run.py`, `app.py`.

### Python interpreter selection

When starting a bot, the manager picks the interpreter in this order:

1. The bot's own venv (`<bot>/.venv`, `<bot>/venv`, or `<bot>/env`).
2. The path entered in **Python Executable (optional)**.
3. The interpreter running the manager.

This means each bot can have its own isolated dependencies — just create a venv inside the bot folder and install its requirements there.

### Update behavior

- Updates are checked against `origin/main` using `git fetch` + `git rev-list HEAD..origin/main`.
- When **Auto update + restart** is enabled, detected updates are pulled with `git pull --ff-only` and any running bot is restarted automatically.
- Click **Update Now** for a one-off update of the selected bot.

## Configuration

User settings are stored in `manager_config.json` (gitignored). A template is provided in `manager_config.example.json`:

```json
{
  "bots_root": "",
  "python_executable": "",
  "update_interval_sec": 120,
  "backup_interval_days": 1,
  "auto_update_restart": true
}
```

To get started, copy the example file:

```powershell
Copy-Item manager_config.example.json manager_config.json
```

The app will also create/update this file automatically when you change settings in the GUI.

## Project Structure

```
Bot_Manager/
├── app.py                       # GUI + bot management logic
├── run_manager.bat              # Windows launcher
├── manager_config.example.json  # Template settings (manager_config.json is gitignored)
├── requirements.txt             # (stdlib only)
├── bot_data/                    # Local backup archives + backup status (gitignored)
├── .gitignore
└── README.md
```

## Publishing to the `main` branch

From the `Bot_Manager` folder:

```powershell
git checkout -b main
git add .
git commit -m "Initial commit: Discord Bot Manager"
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

If a `master` branch already exists and you want `main` instead:

```powershell
git branch -m master main
git push -u origin main
```


