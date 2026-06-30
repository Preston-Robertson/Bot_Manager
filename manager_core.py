"""GUI-free engine for the Bot Manager.

This module is intentionally Tkinter-free so it can run on a headless Linux
container. All UI concerns (Tk, web, CLI) layer on top of `BotManager`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None  # health metrics degrade gracefully when missing

CONFIG_PATH = Path(__file__).with_name("manager_config.json")
BACKUP_ROOT = Path(__file__).with_name("bot_data")
BACKUP_STATUS_PATH = BACKUP_ROOT / "backup_status.json"
BOT_SETTINGS_PATH = BACKUP_ROOT / "bot_settings.json"
MANAGER_DIR = Path(__file__).resolve().parent

ENTRY_CANDIDATES = ["main.py", "bot.py", "run.py", "app.py"]
BACKUP_FILE_EXTENSIONS = {
    ".csv",
    ".db",
    ".db3",
    ".json",
    ".pkl",
    ".pickle",
    ".sqlite",
    ".sqlite3",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".yaml",
    ".yml",
}
BACKUP_EXCLUDED_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules"}

# File-types allowed by the in-browser config editor. Anything outside this
# set (especially binaries / pickles) is hidden from the listing and rejected
# on write. `.env` and `.env.*` are also allowed by filename.
EDITABLE_CONFIG_EXTENSIONS = {
    ".cfg",
    ".env",
    ".ini",
    ".json",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
EDITABLE_FILE_MAX_BYTES = 1_000_000  # 1 MB cap; bigger files are not config

# Binary file-types accepted by the upload endpoint. These are listed in the
# file panel (read-only — the text editor won't open them) and persisted into
# the bot folder. Pickle deserialization is unsafe; only the authenticated
# dashboard user can place these files, and nothing here auto-loads them.
UPLOADABLE_FILE_EXTENSIONS = {
    ".csv",
    ".db",
    ".db3",
    ".pickle",
    ".pkl",
    ".sqlite",
    ".sqlite3",
    ".xls",
    ".xlsm",
    ".xlsx",
}
UPLOAD_FILE_MAX_BYTES = 25_000_000  # 25 MB cap for binary uploads

# Crash-restart policy: how many crashes inside a sliding window count as a
# "restart loop" — once exceeded, the manager stops auto-restarting that bot
# until the user starts it again manually.
CRASH_RESTART_WINDOW_SEC = 600  # 10 minutes
CRASH_RESTART_MAX_ATTEMPTS = 3
CRASH_RESTART_DELAY_SEC = 2.0

LOG_BUFFER_SIZE = 2000
SCHEDULER_TICK_SEC = 3.0
STATUS_REAPER_TICK_SEC = 1.0
BACKUP_CHECK_INTERVAL_SEC = 300  # how often we re-evaluate per-bot backup due-times

# Accept only public-style git URLs we know how to clone without prompting.
# https://host/path[.git], http://..., git://..., or ssh user@host:path.git
_GIT_URL_RE = re.compile(
    r"^(?:https?://[^\s]+|git://[^\s]+|[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[^\s]+)$"
)
_FOLDER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BotInfo:
    name: str
    path: Path
    entry_file: str
    is_git_repo: bool
    update_available: bool = False
    last_update_check: float = 0.0
    last_backup_at: float = 0.0
    process: subprocess.Popen | None = None
    process_reader: threading.Thread | None = None
    # Auto-restart this bot if its subprocess exits non-zero.
    restart_on_crash: bool = False
    # Internal: set just before we deliberately stop the bot so the crash
    # detector doesn't treat the SIGTERM exit as a crash.
    _stop_requested: bool = False

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None


@dataclass
class AppConfig:
    bots_root: str = ""
    python_executable: str = ""
    # Update checks fetch from origin per bot + the manager — kept daily by default
    # so the log isn't dominated by routine "up to date" entries.
    update_interval_sec: int = 86_400
    backup_interval_days: int = 1
    auto_update_restart: bool = True

    @classmethod
    def load(cls) -> "AppConfig":
        if not CONFIG_PATH.exists():
            return cls()
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            backup_interval_days_raw = data.get("backup_interval_days")
            if backup_interval_days_raw is None:
                # Backward compatibility for older configs that stored seconds.
                legacy_backup_sec = int(data.get("backup_interval_sec", 600))
                backup_interval_days = max(1, (legacy_backup_sec + 86399) // 86400)
            else:
                backup_interval_days = max(1, int(backup_interval_days_raw))
            return cls(
                bots_root=str(data.get("bots_root", "")),
                python_executable=str(data.get("python_executable", "")),
                update_interval_sec=max(60, int(data.get("update_interval_sec", 86_400))),
                backup_interval_days=backup_interval_days,
                auto_update_restart=bool(data.get("auto_update_restart", True)),
            )
        except Exception:
            return cls()

    def save(self) -> None:
        payload = {
            "bots_root": self.bots_root,
            "python_executable": self.python_executable,
            "update_interval_sec": self.update_interval_sec,
            "backup_interval_days": self.backup_interval_days,
            "auto_update_restart": self.auto_update_restart,
        }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@dataclass
class LogEntry:
    timestamp: float
    source: str
    message: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "time": datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S"),
            "source": self.source,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class BotManager:
    """All bot discovery, lifecycle, git, and backup logic — UI-free."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig.load()
        self.bots: dict[str, BotInfo] = {}
        self.bots_lock = threading.Lock()

        # Logs: bounded ring buffer + subscriber callables (e.g. WebSockets).
        self._log_buffer: deque[LogEntry] = deque(maxlen=LOG_BUFFER_SIZE)
        self._log_lock = threading.Lock()
        self._log_subscribers: set[Callable[[LogEntry], None]] = set()

        # Background scheduling state.
        self._stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self._reaper_thread: threading.Thread | None = None
        self._update_thread_running = False
        self._backup_thread_running = False
        self._last_global_update_check = 0.0
        self._last_global_backup_check = 0.0
        self._manager_last_update_check = 0.0
        self._backup_jobs_in_progress: set[str] = set()

        self.backup_status = self._load_backup_status()
        # Per-bot settings persisted to disk (currently just restart_on_crash).
        self.bot_settings: dict[str, dict[str, object]] = self._load_bot_settings()
        # psutil.Process per bot, cached so cpu_percent() can compute a delta
        # across snapshots. Keyed by bot name; cleared when a bot stops.
        self._psutil_procs: dict[str, "psutil.Process"] = {}
        # Sliding-window crash timestamps per bot for the restart-loop guard.
        self._crash_history: dict[str, deque[float]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Discover bots and launch background scheduler threads."""
        self.scan_bots()
        self._stop_event.clear()

        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, name="bm-scheduler", daemon=True
        )
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, name="bm-reaper", daemon=True
        )
        self._scheduler_thread.start()
        self._reaper_thread.start()
        self.log("SYSTEM", "Bot Manager started")

    def shutdown(self) -> None:
        """Stop scheduler threads and terminate all running bot subprocesses."""
        self.log("SYSTEM", "Bot Manager shutting down")
        self._stop_event.set()

        with self.bots_lock:
            running = [b for b in self.bots.values() if b.is_running]
        for bot in running:
            try:
                self._stop_bot(bot)
            except Exception as exc:
                self.log(bot.name, f"Error during shutdown stop: {exc}")

        try:
            self.config.save()
        except Exception as exc:
            self.log("SYSTEM", f"Failed to save config on shutdown: {exc}")

    # ------------------------------------------------------------------
    # Logging (pub/sub)
    # ------------------------------------------------------------------

    def log(self, source: str, message: str) -> None:
        entry = LogEntry(timestamp=time.time(), source=source, message=message)
        with self._log_lock:
            self._log_buffer.append(entry)
            subscribers = list(self._log_subscribers)
        # Notify outside the lock so a slow subscriber can't block other writers.
        for cb in subscribers:
            try:
                cb(entry)
            except Exception:
                # A broken subscriber must never crash the engine.
                pass

    def recent_logs(self) -> list[LogEntry]:
        with self._log_lock:
            return list(self._log_buffer)

    def subscribe_logs(self, callback: Callable[[LogEntry], None]) -> None:
        with self._log_lock:
            self._log_subscribers.add(callback)

    def unsubscribe_logs(self, callback: Callable[[LogEntry], None]) -> None:
        with self._log_lock:
            self._log_subscribers.discard(callback)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def update_config(self, **fields) -> AppConfig:
        """Validate + persist config fields coming from the web layer."""
        if "bots_root" in fields:
            self.config.bots_root = str(fields["bots_root"] or "").strip()
        if "python_executable" in fields:
            self.config.python_executable = str(fields["python_executable"] or "").strip()
        if "update_interval_sec" in fields:
            try:
                self.config.update_interval_sec = max(60, int(fields["update_interval_sec"]))
            except (TypeError, ValueError):
                self.config.update_interval_sec = 86_400
        if "backup_interval_days" in fields:
            try:
                self.config.backup_interval_days = max(1, int(fields["backup_interval_days"]))
            except (TypeError, ValueError):
                self.config.backup_interval_days = 1
        if "auto_update_restart" in fields:
            self.config.auto_update_restart = bool(fields["auto_update_restart"])

        self.config.save()
        self.log("SYSTEM", "Settings saved")
        return self.config

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def scan_bots(self) -> dict[str, BotInfo]:
        root_path = Path(self.config.bots_root.strip()) if self.config.bots_root else None
        if not root_path or not root_path.exists() or not root_path.is_dir():
            self.log("SYSTEM", "Bots root folder is invalid")
            with self.bots_lock:
                # Keep existing running bots even if root is now invalid.
                return dict(self.bots)

        discovered: dict[str, BotInfo] = {}
        candidates: list[Path] = []

        # Treat the root itself as a bot if it has an entry file (single-bot case).
        if self._detect_entry_file(root_path) and not self._is_manager_dir(root_path):
            candidates.append(root_path)

        for child in sorted(root_path.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name == "__pycache__":
                continue
            candidates.append(child)

        for child in candidates:
            # Never let the manager discover itself as a bot, even when the
            # manager folder lives under the configured bots_root.
            if self._is_manager_dir(child):
                continue

            entry = self._detect_entry_file(child)
            if not entry:
                continue

            bot = BotInfo(
                name=child.name,
                path=child,
                entry_file=entry,
                is_git_repo=(child / ".git").exists(),
            )

            if bot.name in self.bots and self.bots[bot.name].is_running:
                existing = self.bots[bot.name]
                bot.process = existing.process
                bot.process_reader = existing.process_reader
                bot.update_available = existing.update_available
                bot.last_update_check = existing.last_update_check

            status = self.backup_status.get(bot.name, {})
            try:
                bot.last_backup_at = float(status.get("last_backup_at", 0.0))
            except (TypeError, ValueError):
                bot.last_backup_at = 0.0

            # Restore persisted per-bot settings.
            settings = self.bot_settings.get(bot.name, {})
            bot.restart_on_crash = bool(settings.get("restart_on_crash", False))

            discovered[bot.name] = bot

        with self.bots_lock:
            self.bots = discovered

        self.log("SYSTEM", f"Scan complete: found {len(discovered)} bot(s)")
        return discovered

    @staticmethod
    def _detect_entry_file(path: Path) -> str | None:
        for candidate in ENTRY_CANDIDATES:
            if (path / candidate).exists():
                return candidate
        return None

    @staticmethod
    def _is_manager_dir(path: Path) -> bool:
        try:
            return path.resolve() == MANAGER_DIR
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Process control
    # ------------------------------------------------------------------

    def _python_command(self, bot: BotInfo | None = None) -> str:
        # 1. Per-bot venv wins. Check both Windows and POSIX layouts.
        if bot is not None:
            for venv_dir in (".venv", "venv", "env"):
                win = bot.path / venv_dir / "Scripts" / "python.exe"
                if win.exists():
                    return str(win)
                posix = bot.path / venv_dir / "bin" / "python"
                if posix.exists():
                    return str(posix)

        # 2. User-configured python.
        if self.config.python_executable:
            return self.config.python_executable

        # 3. Fallback to the manager's interpreter.
        return sys.executable

    def start_bot(self, name: str) -> tuple[bool, str]:
        with self.bots_lock:
            bot = self.bots.get(name)
        if not bot:
            return False, f"Unknown bot: {name}"
        self._start_bot(bot)
        return True, "started" if bot.is_running else "failed to start"

    def stop_bot(self, name: str) -> tuple[bool, str]:
        with self.bots_lock:
            bot = self.bots.get(name)
        if not bot:
            return False, f"Unknown bot: {name}"
        self._stop_bot(bot)
        return True, "stopped"

    def restart_bot(self, name: str) -> tuple[bool, str]:
        with self.bots_lock:
            bot = self.bots.get(name)
        if not bot:
            return False, f"Unknown bot: {name}"
        self._restart_bot(bot)
        return True, "restarted"

    def _start_bot(self, bot: BotInfo) -> None:
        if bot.is_running:
            self.log(bot.name, "Already running")
            return

        command = [self._python_command(bot), bot.entry_file]
        # Don't leak the manager's own secrets into bot processes. A bot has
        # no legitimate need for BOTMGR_TOKEN / BOTMGR_HOST / BOTMGR_PORT.
        bot_env = {k: v for k, v in os.environ.items() if not k.startswith("BOTMGR_")}
        try:
            process = subprocess.Popen(
                command,
                cwd=str(bot.path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=bot_env,
            )
        except Exception as exc:
            self.log(bot.name, f"Failed to start: {exc}")
            return

        bot.process = process
        bot._stop_requested = False
        # Seed the psutil handle so cpu_percent() has a baseline for the next
        # snapshot. psutil is optional; missing it just disables health metrics.
        if psutil is not None:
            try:
                proc = psutil.Process(process.pid)
                proc.cpu_percent(interval=None)  # prime the delta
                self._psutil_procs[bot.name] = proc
            except Exception:
                self._psutil_procs.pop(bot.name, None)
        reader = threading.Thread(
            target=self._read_process_output,
            args=(bot.name, process),
            daemon=True,
        )
        bot.process_reader = reader
        reader.start()
        self.log(bot.name, f"Started ({' '.join(command)})")

    def _stop_bot(self, bot: BotInfo) -> None:
        if not bot.is_running:
            self.log(bot.name, "Already stopped")
            return

        assert bot.process is not None
        bot._stop_requested = True
        bot.process.terminate()
        try:
            bot.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bot.process.kill()

        bot.process = None
        bot.process_reader = None
        self._psutil_procs.pop(bot.name, None)
        self.log(bot.name, "Stopped")

    def _restart_bot(self, bot: BotInfo) -> None:
        self.log(bot.name, "Restarting")
        if bot.is_running:
            self._stop_bot(bot)
        self._start_bot(bot)

    def _read_process_output(self, bot_name: str, process: subprocess.Popen) -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                self.log(bot_name, line.rstrip())
        except Exception as exc:
            self.log(bot_name, f"Log stream error: {exc}")
        finally:
            # Wait briefly so `returncode` is populated even if the stream
            # closed before the OS marked the process as exited.
            rc = process.poll()
            if rc is None:
                try:
                    process.wait(timeout=2)
                    rc = process.returncode
                except Exception:
                    rc = None
            self._on_process_exit(bot_name, rc)

    def _on_process_exit(self, bot_name: str, returncode: int | None) -> None:
        """Log the exit and, if configured, schedule a bounded auto-restart.

        On Linux a SIGTERM exit shows up as a negative returncode (e.g. -15),
        which we treat as a crash unless the manager set `_stop_requested`
        on the bot just before calling .terminate().
        """
        with self.bots_lock:
            bot = self.bots.get(bot_name)

        if returncode is None:
            self.log(bot_name, "Process exited (returncode unknown)")
            crashed = False
        elif returncode == 0:
            self.log(bot_name, "Process exited cleanly")
            crashed = False
        else:
            crashed = bot is not None and not bot._stop_requested
            if crashed:
                self.log(bot_name, f"Process crashed (exit code {returncode})")
            else:
                self.log(bot_name, f"Process exited (signal/exit code {returncode})")

        # Reset the stop flag for the next start cycle.
        if bot is not None:
            bot._stop_requested = False

        if crashed and bot is not None and bot.restart_on_crash and not self._stop_event.is_set():
            self._handle_crash_restart(bot)

    def _handle_crash_restart(self, bot: BotInfo) -> None:
        """Auto-restart a crashed bot, capped at N attempts inside a sliding window."""
        now = time.time()
        history = self._crash_history.setdefault(bot.name, deque())
        # Prune entries that fell out of the window.
        cutoff = now - CRASH_RESTART_WINDOW_SEC
        while history and history[0] < cutoff:
            history.popleft()
        history.append(now)

        if len(history) > CRASH_RESTART_MAX_ATTEMPTS:
            self.log(
                bot.name,
                (
                    f"Restart loop detected ({len(history)} crashes in "
                    f"{CRASH_RESTART_WINDOW_SEC // 60} min). Auto-restart disabled "
                    "until manual start."
                ),
            )
            # Stop the runaway loop; user must start the bot themselves.
            bot.restart_on_crash = False
            self.bot_settings.setdefault(bot.name, {})["restart_on_crash"] = False
            try:
                self._save_bot_settings()
            except OSError:
                pass
            return

        attempt = len(history)
        self.log(
            bot.name,
            f"Auto-restart in {CRASH_RESTART_DELAY_SEC:.0f}s (attempt {attempt}/{CRASH_RESTART_MAX_ATTEMPTS})",
        )
        # Daemon thread so a shutdown can race ahead without blocking.
        def _delayed_restart() -> None:
            if self._stop_event.wait(CRASH_RESTART_DELAY_SEC):
                return  # manager is shutting down
            with self.bots_lock:
                live = self.bots.get(bot.name)
            if live is None or live.is_running:
                return
            self._start_bot(live)

        threading.Thread(target=_delayed_restart, daemon=True).start()

    # ------------------------------------------------------------------
    # Git updates
    # ------------------------------------------------------------------

    def _run_git(self, cwd: Path, args: list[str], timeout: int = 45) -> tuple[bool, str]:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            return completed.returncode == 0, output.strip()
        except Exception as exc:
            return False, str(exc)

    def check_updates(self) -> None:
        """Public: launch a one-shot update check on a background thread."""
        if self._update_thread_running:
            return
        threading.Thread(target=self._check_updates_worker, daemon=True).start()

    def _check_updates_worker(self) -> None:
        self._update_thread_running = True
        try:
            with self.bots_lock:
                bot_names = list(self.bots.keys())

            for bot_name in bot_names:
                with self.bots_lock:
                    bot = self.bots.get(bot_name)
                if not bot or not bot.is_git_repo:
                    continue

                updated = self._has_remote_update(bot)
                bot.update_available = updated
                bot.last_update_check = time.time()

                if updated:
                    self.log(bot.name, "Update detected on origin/main")
                    if self.config.auto_update_restart:
                        self._update_bot_worker(bot.name, silent=True)
                # No log on the "no update" path — that was the main source of
                # routine noise in the log stream.

            # Piggyback the manager self-update check on the same cadence.
            # We only *check* automatically; applying + restart is always manual.
            try:
                self.check_manager_update()
            except Exception as exc:
                self.log("MANAGER", f"Self-update check error: {exc}")
        finally:
            self._update_thread_running = False

    def _has_remote_update(self, bot: BotInfo) -> bool:
        ok, out = self._run_git(bot.path, ["fetch", "origin", "main"])
        if not ok:
            self.log(bot.name, f"Update check failed: {out}")
            return False

        ok, out = self._run_git(bot.path, ["rev-list", "--count", "HEAD..origin/main"])
        if not ok:
            self.log(bot.name, f"Unable to compare HEAD with origin/main: {out}")
            return False

        try:
            count = int(out.strip().splitlines()[-1])
            return count > 0
        except Exception:
            self.log(bot.name, f"Unexpected git output: {out}")
            return False

    def update_bot(self, name: str) -> tuple[bool, str]:
        """Public: blocking update of a single bot (call from a threadpool)."""
        self._update_bot_worker(name)
        return True, "update finished"

    def _update_bot_worker(self, bot_name: str, silent: bool = False) -> None:
        with self.bots_lock:
            bot = self.bots.get(bot_name)
        if not bot:
            return
        if not bot.is_git_repo:
            if not silent:
                self.log(bot.name, "Not a git repo; cannot update")
            return

        was_running = bot.is_running

        ok, output = self._run_git(bot.path, ["pull", "origin", "main", "--ff-only"], timeout=90)
        if not ok:
            self.log(bot.name, f"Update failed: {output}")
            return

        bot.update_available = False
        self.log(bot.name, "Updated from origin/main")

        if self.config.auto_update_restart and was_running:
            self.log(bot.name, "Restarting after update")
            self._restart_bot(bot)

    # ------------------------------------------------------------------
    # Manager self-update
    # ------------------------------------------------------------------

    def _manager_is_git_repo(self) -> bool:
        return (MANAGER_DIR / ".git").exists()

    def manager_status(self) -> dict:
        """Snapshot of the manager's own git checkout for the dashboard."""
        info: dict = {
            "is_git_repo": False,
            "branch": "",
            "head": "",
            "head_short": "",
            "dirty": False,
            "dirty_files": [],
            "update_available": False,
            "ahead": 0,
            "behind": 0,
            "last_check": self._manager_last_update_check,
            "remote": "",
            "path": str(MANAGER_DIR),
        }
        if not self._manager_is_git_repo():
            return info
        info["is_git_repo"] = True

        ok, out = self._run_git(MANAGER_DIR, ["rev-parse", "--abbrev-ref", "HEAD"])
        if ok:
            info["branch"] = out.strip()
        ok, out = self._run_git(MANAGER_DIR, ["rev-parse", "HEAD"])
        if ok:
            info["head"] = out.strip()
            info["head_short"] = info["head"][:8]
        ok, out = self._run_git(MANAGER_DIR, ["status", "--porcelain"])
        if ok:
            info["dirty"] = bool(out.strip())
            # Porcelain lines look like "XY path" or "XY orig -> new" (rename).
            # Take everything after the 2-char status code, strip leading space,
            # then keep only the destination side of a rename. Cap the list so we
            # don't ship huge payloads.
            files: list[str] = []
            for line in out.splitlines():
                stripped = line[2:].lstrip()
                if not stripped:
                    continue
                if " -> " in stripped:
                    stripped = stripped.split(" -> ", 1)[1]
                files.append(stripped)
            info["dirty_files"] = files[:50]
        ok, out = self._run_git(MANAGER_DIR, ["config", "--get", "remote.origin.url"])
        if ok:
            info["remote"] = out.strip()

        branch = info["branch"] or "main"
        ok, out = self._run_git(
            MANAGER_DIR,
            ["rev-list", "--count", "--left-right", f"HEAD...origin/{branch}"],
        )
        if ok and out:
            parts = out.split()
            if len(parts) == 2:
                try:
                    info["ahead"] = int(parts[0])
                    info["behind"] = int(parts[1])
                    info["update_available"] = info["behind"] > 0
                except ValueError:
                    pass
        return info

    def check_manager_update(self) -> dict:
        """Fetch origin for the manager repo, log the result, return refreshed status."""
        if not self._manager_is_git_repo():
            self.log("MANAGER", "Self-update check skipped: not a git checkout")
            return self.manager_status()

        ok, out = self._run_git(MANAGER_DIR, ["rev-parse", "--abbrev-ref", "HEAD"])
        branch = out.strip() if ok and out.strip() else "main"

        ok, out = self._run_git(MANAGER_DIR, ["fetch", "origin", branch], timeout=45)
        self._manager_last_update_check = time.time()
        if not ok:
            self.log("MANAGER", f"Self-update check failed: {out}")

        status = self.manager_status()
        if status["update_available"]:
            self.log(
                "MANAGER",
                f"Update available: {status['behind']} commit(s) behind origin/{branch}",
            )
        elif ok:
            self.log("MANAGER", f"Self-update check: up to date (origin/{branch})")
        return status

    def update_manager(self, *, force: bool = False) -> tuple[bool, str]:
        """git pull --ff-only the manager itself. Caller decides whether to restart.

        When ``force=True`` and the working tree is dirty, local changes are
        stashed (including untracked files) before the pull. The stash is
        preserved so nothing is destroyed; recover with ``git stash list`` and
        ``git stash pop`` from a shell if needed.
        """
        if not self._manager_is_git_repo():
            return False, "Manager directory is not a git checkout"
        status = self.manager_status()
        stashed = False
        if status["dirty"]:
            if not force:
                return (
                    False,
                    "Manager working tree has uncommitted changes; refusing to update",
                )
            stash_msg = f"botmgr auto-stash {time.strftime('%Y-%m-%d %H:%M:%S')}"
            ok, out = self._run_git(
                MANAGER_DIR, ["stash", "push", "-u", "-m", stash_msg], timeout=60
            )
            if not ok:
                self.log("MANAGER", f"Self-update stash failed: {out}")
                return False, f"git stash failed: {out or 'unknown error'}"
            stashed = True
            self.log("MANAGER", f"Stashed local changes before update: {stash_msg}")
        branch = status["branch"] or "main"
        ok, out = self._run_git(
            MANAGER_DIR, ["pull", "origin", branch, "--ff-only"], timeout=120
        )
        if not ok:
            self.log("MANAGER", f"Self-update failed: {out}")
            hint = " Local changes are preserved in `git stash`." if stashed else ""
            return False, (out or "git pull failed") + hint
        msg = f"Self-update applied from origin/{branch}"
        if stashed:
            msg += " (local changes preserved in stash)"
        self.log("MANAGER", msg)
        user_msg = "Manager updated. Restart to apply the new code."
        if stashed:
            user_msg += " Local changes were stashed; use `git stash list` on the host to inspect."
        return True, user_msg

    def restart_manager(self) -> tuple[bool, str]:
        """Stop running bots, persist config, then re-exec this Python process."""
        self.log("MANAGER", "Restart requested — stopping bots and re-execing")

        with self.bots_lock:
            running = [b for b in self.bots.values() if b.is_running]
        for bot in running:
            try:
                self._stop_bot(bot)
            except Exception as exc:
                self.log(bot.name, f"Error stopping before restart: {exc}")

        try:
            self.config.save()
        except Exception as exc:
            self.log("MANAGER", f"Failed to save config before restart: {exc}")

        # Defer the exec so the HTTP response can flush to the client.
        threading.Thread(target=self._exec_restart, daemon=True).start()
        return True, "Restart scheduled"

    def _exec_restart(self) -> None:
        time.sleep(1.0)
        python = sys.executable
        script = str(MANAGER_DIR / "app.py")
        try:
            # On POSIX this replaces the process image in-place — clean for systemd.
            # On Windows it spawns a new process and exits the old one (acceptable).
            os.execv(python, [python, script])
        except Exception as exc:
            self.log("MANAGER", f"Restart exec failed: {exc}")

    # ------------------------------------------------------------------
    # Add bot by cloning a git repo
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_repo_folder_name(url: str) -> str | None:
        """Extract a safe folder name from a git URL (rejects unsafe names)."""
        # Strip a trailing ".git" and any trailing slashes.
        tail = url.rstrip("/").split("/")[-1]
        if ":" in tail and "@" in url and "://" not in url:
            # ssh-style "user@host:path/repo.git" - the last ":" segment.
            tail = tail.split(":")[-1].split("/")[-1]
        if tail.endswith(".git"):
            tail = tail[:-4]
        tail = tail.strip()
        if not tail or not _FOLDER_NAME_RE.match(tail):
            return None
        return tail

    def _venv_python_path(self, venv_dir: Path) -> Path | None:
        for candidate in (venv_dir / "Scripts" / "python.exe", venv_dir / "bin" / "python"):
            if candidate.exists():
                return candidate
        return None

    def add_bot_from_git(
        self,
        repo_url: str,
        branch: str | None = None,
        install_deps: bool = True,
    ) -> tuple[bool, str]:
        """Clone a public git repo into bots_root and (optionally) set up its venv.

        Returns (ok, message). Streams progress to the live log.
        """
        repo_url = (repo_url or "").strip()
        branch = (branch or "").strip() or None

        if not repo_url:
            return False, "Repository URL is required"
        if not _GIT_URL_RE.match(repo_url):
            return False, "Repository URL is not a recognized git URL"
        if branch and not re.match(r"^[A-Za-z0-9._/-]+$", branch):
            return False, "Branch name contains invalid characters"

        bots_root = self.config.bots_root.strip()
        if not bots_root:
            return False, "Bots root folder is not configured"
        root_path = Path(bots_root)
        if not root_path.is_dir():
            return False, f"Bots root does not exist: {bots_root}"

        folder = self._derive_repo_folder_name(repo_url)
        if not folder:
            return False, "Could not derive a safe folder name from the URL"

        target_dir = (root_path / folder).resolve()
        # Sanity: stay inside bots_root.
        try:
            target_dir.relative_to(root_path.resolve())
        except ValueError:
            return False, "Resolved path escapes the bots root folder"

        if target_dir.exists():
            return False, f"Target folder already exists: {target_dir}"

        self.log("SYSTEM", f"Cloning {repo_url} -> {target_dir}")

        clone_cmd = ["git", "clone", "--depth", "1"]
        if branch:
            clone_cmd.extend(["--branch", branch])
        clone_cmd.extend([repo_url, str(target_dir)])

        try:
            completed = subprocess.run(
                clone_cmd, capture_output=True, text=True, timeout=300
            )
        except Exception as exc:
            return False, f"git clone failed to launch: {exc}"

        if completed.returncode != 0:
            output = (completed.stderr or completed.stdout or "").strip()
            self.log("SYSTEM", f"Clone failed: {output}")
            return False, f"git clone failed: {output or 'unknown error'}"

        self.log("SYSTEM", f"Clone complete: {target_dir}")

        entry = self._detect_entry_file(target_dir)
        if not entry:
            # Clean up so we don't leave an unusable folder lying around.
            self.log(
                "SYSTEM",
                "Cloned repo has no recognized entry file "
                f"({', '.join(ENTRY_CANDIDATES)}); removing.",
            )
            self._remove_tree(target_dir)
            return False, (
                "Cloned repo has no recognized entry file "
                f"({', '.join(ENTRY_CANDIDATES)})"
            )

        if install_deps:
            self._setup_bot_venv(target_dir)

        # Refresh discovery so the new bot appears in the table.
        self.scan_bots()
        return True, f"Added bot '{folder}'"

    def _setup_bot_venv(self, bot_dir: Path) -> None:
        """Create a .venv inside the bot and install requirements.txt if present."""
        venv_dir = bot_dir / ".venv"
        self.log("SYSTEM", f"Creating venv in {venv_dir}")
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except Exception as exc:
            self.log("SYSTEM", f"venv creation failed to launch: {exc}")
            return
        if completed.returncode != 0:
            output = (completed.stderr or completed.stdout or "").strip()
            self.log("SYSTEM", f"venv creation failed: {output}")
            return

        py = self._venv_python_path(venv_dir)
        if py is None:
            self.log("SYSTEM", "venv created but python executable not found")
            return

        req = bot_dir / "requirements.txt"
        if not req.exists():
            self.log("SYSTEM", "No requirements.txt; skipping pip install")
            return

        self.log("SYSTEM", f"Installing requirements from {req.name}")
        try:
            completed = subprocess.run(
                [str(py), "-m", "pip", "install", "-r", str(req)],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except Exception as exc:
            self.log("SYSTEM", f"pip install failed to launch: {exc}")
            return
        if completed.returncode != 0:
            output = (completed.stderr or completed.stdout or "").strip().splitlines()
            tail = "\n".join(output[-10:]) if output else "unknown error"
            self.log("SYSTEM", f"pip install failed:\n{tail}")
            return

        self.log("SYSTEM", "Dependencies installed")

    @staticmethod
    def _remove_tree(path: Path) -> None:
        """Best-effort recursive delete (Windows-safe for read-only .git files)."""
        import shutil
        import stat

        def _on_rm_error(func, target, _exc_info):
            try:
                Path(target).chmod(stat.S_IWRITE)
                func(target)
            except Exception:
                pass

        try:
            shutil.rmtree(path, onerror=_on_rm_error)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Per-bot config file editor
    # ------------------------------------------------------------------

    @staticmethod
    def _is_editable_filename(name: str) -> bool:
        """True if `name` is a single-segment editable config filename."""
        if not name:
            return False
        # No path separators or traversal allowed.
        if "/" in name or "\\" in name or ".." in name:
            return False
        # Skip files inside excluded subtrees (we never list there, but be safe).
        if name in BACKUP_EXCLUDED_DIRS:
            return False
        # .env and .env.* are allowed even though they have no "real" extension.
        if name == ".env" or name.startswith(".env."):
            return True
        # Standard extension check.
        suffix = Path(name).suffix.lower()
        return suffix in EDITABLE_CONFIG_EXTENSIONS

    @classmethod
    def _is_editable_path(cls, rel_path: str) -> bool:
        """True if `rel_path` is a forward-slash subpath ending in an editable filename."""
        if not rel_path or "\\" in rel_path:
            return False
        parts = rel_path.split("/")
        for p in parts[:-1]:
            if not p or p in (".", ".."):
                return False
            if p in BACKUP_EXCLUDED_DIRS:
                return False
            if ":" in p or not _FOLDER_NAME_RE.match(p):
                return False
        return cls._is_editable_filename(parts[-1])

    @staticmethod
    def _is_uploadable_filename(name: str) -> bool:
        """True if `name` is a single-segment filename allowed via the upload endpoint."""
        if not name:
            return False
        if "/" in name or "\\" in name or ".." in name:
            return False
        if name in BACKUP_EXCLUDED_DIRS:
            return False
        suffix = Path(name).suffix.lower()
        return suffix in UPLOADABLE_FILE_EXTENSIONS

    @classmethod
    def _is_uploadable_path(cls, rel_path: str) -> bool:
        """True if `rel_path` is a forward-slash subpath ending in an uploadable filename."""
        if not rel_path or "\\" in rel_path:
            return False
        parts = rel_path.split("/")
        for p in parts[:-1]:
            if not p or p in (".", ".."):
                return False
            if p in BACKUP_EXCLUDED_DIRS:
                return False
            if ":" in p or not _FOLDER_NAME_RE.match(p):
                return False
        return cls._is_uploadable_filename(parts[-1])

    @staticmethod
    def _is_safe_folder_path(rel_path: str) -> bool:
        """True if `rel_path` is a forward-slash folder path with safe segments."""
        if not rel_path or "\\" in rel_path:
            return False
        parts = rel_path.split("/")
        for p in parts:
            if not p or p in (".", ".."):
                return False
            if p in BACKUP_EXCLUDED_DIRS:
                return False
            if ":" in p or not _FOLDER_NAME_RE.match(p):
                return False
        return True

    def _resolve_bot_file(self, bot_name: str, file_path: str) -> Path | None:
        """Return the absolute path of an editable file inside a bot folder.

        Returns None if the bot is unknown, the path is not allowed, or
        the resolved path would escape the bot folder.
        """
        with self.bots_lock:
            bot = self.bots.get(bot_name)
        if not bot:
            return None
        if not self._is_editable_path(file_path):
            return None
        bot_root = bot.path.resolve()
        candidate = (bot_root / file_path).resolve()
        try:
            candidate.relative_to(bot_root)
        except ValueError:
            return None
        return candidate

    def _resolve_bot_upload_path(self, bot_name: str, file_path: str) -> Path | None:
        """Return the absolute path for an uploadable (binary) file inside a bot folder.

        Mirrors `_resolve_bot_file` but uses the uploadable extension list.
        """
        with self.bots_lock:
            bot = self.bots.get(bot_name)
        if not bot:
            return None
        if not self._is_uploadable_path(file_path):
            return None
        bot_root = bot.path.resolve()
        candidate = (bot_root / file_path).resolve()
        try:
            candidate.relative_to(bot_root)
        except ValueError:
            return None
        return candidate

    def _resolve_bot_any_path(self, bot_name: str, file_path: str) -> Path | None:
        """Resolve any listed file (editable OR uploadable). Used by download/delete."""
        # Try editable first; fall back to uploadable. Both share the same
        # path-safety primitives, so this is just an OR over allow-lists.
        return (
            self._resolve_bot_file(bot_name, file_path)
            or self._resolve_bot_upload_path(bot_name, file_path)
        )

    def list_config_files(
        self,
        bot_name: str,
        max_depth: int = 5,
        max_files: int = 500,
    ) -> list[dict] | None:
        """Recursively list editable config files under the bot folder.

        Skips excluded subtrees (`.git`, `.venv`, etc.). Capped at `max_depth`
        levels and `max_files` entries to keep the UI responsive on big trees.
        Each entry has an `editable` flag — uploaded binaries (e.g. `.pkl`,
        sqlite, xlsx) are listed but not openable in the text editor.
        """
        with self.bots_lock:
            bot = self.bots.get(bot_name)
        if not bot:
            return None

        bot_root = bot.path
        out: list[dict] = []

        def walk(folder: Path, rel: str, depth: int) -> None:
            if depth > max_depth or len(out) >= max_files:
                return
            try:
                entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return
            for entry in entries:
                if len(out) >= max_files:
                    return
                if entry.name in BACKUP_EXCLUDED_DIRS:
                    continue
                child_rel = f"{rel}/{entry.name}" if rel else entry.name
                if entry.is_dir():
                    walk(entry, child_rel, depth + 1)
                    continue
                if not entry.is_file():
                    continue
                editable = self._is_editable_filename(entry.name)
                uploadable = self._is_uploadable_filename(entry.name)
                if not (editable or uploadable):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                out.append(
                    {
                        "path": child_rel,
                        "name": entry.name,
                        "folder": rel,
                        "size": stat.st_size,
                        "size_human": self._format_bytes(stat.st_size),
                        "mtime": stat.st_mtime,
                        "mtime_human": datetime.fromtimestamp(stat.st_mtime).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "editable": editable,
                    }
                )

        walk(bot_root, "", 0)
        out.sort(key=lambda f: f["path"].lower())
        return out

    def read_config_file(self, bot_name: str, file_path: str) -> tuple[bool, str]:
        """Read an editable config file's text. Returns (ok, content_or_error)."""
        path = self._resolve_bot_file(bot_name, file_path)
        if path is None:
            return False, "File not allowed"
        if not path.is_file():
            return False, "File not found"
        try:
            size = path.stat().st_size
        except OSError as exc:
            return False, f"Stat failed: {exc}"
        if size > EDITABLE_FILE_MAX_BYTES:
            return False, (
                f"File is too large to edit ({size} bytes; "
                f"limit {EDITABLE_FILE_MAX_BYTES})"
            )
        try:
            return True, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return False, "File is not valid UTF-8 text"
        except OSError as exc:
            return False, f"Read failed: {exc}"

    def write_config_file(
        self, bot_name: str, file_path: str, content: str
    ) -> tuple[bool, str]:
        """Write text to an editable config file. Creates parent folders if missing."""
        path = self._resolve_bot_file(bot_name, file_path)
        if path is None:
            return False, "File not allowed"
        if len(content.encode("utf-8")) > EDITABLE_FILE_MAX_BYTES:
            return False, (
                f"Content too large; limit {EDITABLE_FILE_MAX_BYTES} bytes"
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
        except OSError as exc:
            return False, f"Write failed: {exc}"
        self.log(bot_name, f"Config file updated: {file_path}")
        return True, "saved"

    def upload_bot_file(
        self, bot_name: str, file_path: str, data: bytes
    ) -> tuple[bool, str]:
        """Write raw bytes to an uploadable file. Creates parent folders if missing.

        Used for binary payloads (pickles, sqlite, xlsx, …) that the text-only
        editor can't handle. Caller is expected to have already enforced the
        upload size cap, but we re-check defensively.
        """
        path = self._resolve_bot_upload_path(bot_name, file_path)
        if path is None:
            return False, "File not allowed"
        if len(data) > UPLOAD_FILE_MAX_BYTES:
            return False, (
                f"Upload too large; limit {UPLOAD_FILE_MAX_BYTES} bytes"
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            return False, f"Write failed: {exc}"
        self.log(
            bot_name, f"File uploaded: {file_path} ({self._format_bytes(len(data))})"
        )
        return True, "uploaded"

    def delete_bot_file(self, bot_name: str, file_path: str) -> tuple[bool, str]:
        """Delete an editable or uploadable file inside a bot folder."""
        path = self._resolve_bot_any_path(bot_name, file_path)
        if path is None:
            return False, "File not allowed"
        if not path.is_file():
            return False, "File not found"
        try:
            path.unlink()
        except OSError as exc:
            return False, f"Delete failed: {exc}"
        self.log(bot_name, f"File deleted: {file_path}")
        return True, "deleted"

    def create_bot_folder(self, bot_name: str, rel_path: str) -> tuple[bool, str]:
        """Create a folder (mkdir -p) inside a bot's directory tree."""
        with self.bots_lock:
            bot = self.bots.get(bot_name)
        if not bot:
            return False, "Unknown bot"
        if not self._is_safe_folder_path(rel_path):
            return False, "Folder path is invalid"
        bot_root = bot.path.resolve()
        target = (bot_root / rel_path).resolve()
        try:
            target.relative_to(bot_root)
        except ValueError:
            return False, "Folder would escape the bot folder"
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"mkdir failed: {exc}"
        self.log(bot_name, f"Folder created: {rel_path}")
        return True, "created"

    # ------------------------------------------------------------------
    # Backups
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_name(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()) or "bot"

    @staticmethod
    def _format_bytes(size_bytes: int) -> str:
        size = float(max(0, size_bytes))
        units = ["B", "KB", "MB", "GB", "TB"]
        unit_idx = 0
        while size >= 1024 and unit_idx < len(units) - 1:
            size /= 1024
            unit_idx += 1
        if unit_idx == 0:
            return f"{int(size)} {units[unit_idx]}"
        return f"{size:.2f} {units[unit_idx]}"

    def _get_backup_storage_bytes(self, bot_name: str) -> int:
        safe_bot_name = self._sanitize_name(bot_name)
        bot_backup_dir = BACKUP_ROOT / safe_bot_name
        if not bot_backup_dir.exists() or not bot_backup_dir.is_dir():
            return 0
        total = 0
        for file_path in bot_backup_dir.rglob("*.zip"):
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
        return total

    def _get_backup_health(self, bot_name: str) -> str:
        status = self.backup_status.get(bot_name, {})
        last_success_at = float(status.get("last_backup_at", 0.0) or 0.0)
        last_failure_at = float(status.get("last_failure_at", 0.0) or 0.0)

        if last_failure_at > last_success_at:
            return "Failed"
        if last_success_at <= 0:
            return "Overdue"

        now = time.time()
        interval_seconds = self.config.backup_interval_days * 86400
        age = max(0.0, now - last_success_at)

        if age >= interval_seconds:
            return "Overdue"
        if age >= interval_seconds * 0.8:
            return "Due Soon"
        return "Healthy"

    def _load_backup_status(self) -> dict[str, dict[str, object]]:
        if not BACKUP_STATUS_PATH.exists():
            return {}
        try:
            data = json.loads(BACKUP_STATUS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_backup_status(self) -> None:
        BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        BACKUP_STATUS_PATH.write_text(
            json.dumps(self.backup_status, indent=2), encoding="utf-8"
        )

    def _load_bot_settings(self) -> dict[str, dict[str, object]]:
        if not BOT_SETTINGS_PATH.exists():
            return {}
        try:
            data = json.loads(BOT_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_bot_settings(self) -> None:
        BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        BOT_SETTINGS_PATH.write_text(
            json.dumps(self.bot_settings, indent=2), encoding="utf-8"
        )

    def set_bot_settings(
        self, bot_name: str, *, restart_on_crash: bool | None = None
    ) -> tuple[bool, str]:
        """Update per-bot settings and persist them. Currently just `restart_on_crash`."""
        with self.bots_lock:
            bot = self.bots.get(bot_name)
        if not bot:
            return False, "Unknown bot"
        entry = dict(self.bot_settings.get(bot_name, {}))
        if restart_on_crash is not None:
            entry["restart_on_crash"] = bool(restart_on_crash)
            bot.restart_on_crash = bool(restart_on_crash)
        self.bot_settings[bot_name] = entry
        try:
            self._save_bot_settings()
        except OSError as exc:
            return False, f"Save failed: {exc}"
        self.log(bot_name, f"Settings updated: restart_on_crash={bot.restart_on_crash}")
        return True, "saved"

    def backup_bot(self, name: str, reason: str = "manual") -> tuple[bool, str]:
        with self.bots_lock:
            bot = self.bots.get(name)
        if not bot:
            return False, f"Unknown bot: {name}"
        self._start_backup_job(name, reason=reason)
        return True, "backup queued"

    def backup_all(self, reason: str = "manual") -> int:
        with self.bots_lock:
            bot_names = sorted(self.bots.keys())
        for bot_name in bot_names:
            self._start_backup_job(bot_name, reason=reason)
        return len(bot_names)

    def _periodic_backup_worker(self, backup_interval_seconds: int) -> None:
        self._backup_thread_running = True
        try:
            now = time.time()
            with self.bots_lock:
                bot_names = sorted(self.bots.keys())
            for bot_name in bot_names:
                status = self.backup_status.get(bot_name, {})
                last_backup = float(status.get("last_backup_at", 0.0) or 0.0)
                if now - last_backup >= backup_interval_seconds:
                    self._start_backup_job(bot_name, reason="scheduled")
        finally:
            self._backup_thread_running = False

    def _start_backup_job(self, bot_name: str, reason: str) -> None:
        if bot_name in self._backup_jobs_in_progress:
            return
        self._backup_jobs_in_progress.add(bot_name)
        threading.Thread(
            target=self._backup_bot_worker, args=(bot_name, reason), daemon=True
        ).start()

    def _backup_bot_worker(self, bot_name: str, reason: str) -> None:
        try:
            with self.bots_lock:
                bot = self.bots.get(bot_name)
            if not bot:
                return

            files = self._find_backup_files(bot.path)
            if not files:
                self.log(bot.name, "Backup skipped: no matching data files found")
                return

            safe_bot_name = self._sanitize_name(bot.name)
            target_dir = BACKUP_ROOT / safe_bot_name
            target_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_path = target_dir / f"{safe_bot_name}_{timestamp}.zip"

            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for file_path in files:
                    rel = file_path.relative_to(bot.path)
                    zf.write(file_path, arcname=str(rel))

            now = time.time()
            bot.last_backup_at = now
            previous = self.backup_status.get(bot.name, {})
            self.backup_status[bot.name] = {
                "last_backup_at": now,
                "last_backup_file": str(zip_path),
                "files_count": len(files),
                "reason": reason,
                "last_result": "success",
                "last_failure_at": float(previous.get("last_failure_at", 0.0) or 0.0),
                "last_error": "",
            }
            self._save_backup_status()
            self.log(
                bot.name,
                f"Backup complete: {zip_path.name} ({len(files)} file(s), reason={reason})",
            )
        except Exception as exc:
            previous = self.backup_status.get(bot_name, {})
            self.backup_status[bot_name] = {
                "last_backup_at": float(previous.get("last_backup_at", 0.0) or 0.0),
                "last_backup_file": str(previous.get("last_backup_file", "")),
                "files_count": int(previous.get("files_count", 0) or 0),
                "reason": reason,
                "last_result": "failed",
                "last_failure_at": time.time(),
                "last_error": str(exc),
            }
            self._save_backup_status()
            self.log(bot_name, f"Backup failed: {exc}")
        finally:
            self._backup_jobs_in_progress.discard(bot_name)

    def _find_backup_files(self, bot_root: Path) -> list[Path]:
        matches: list[Path] = []
        for path in bot_root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in BACKUP_EXCLUDED_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in BACKUP_FILE_EXTENSIONS:
                matches.append(path)
        return sorted(matches)

    def list_backups(self, bot_name: str) -> list[dict]:
        """Return metadata for each .zip backup for a bot."""
        safe_bot_name = self._sanitize_name(bot_name)
        bot_backup_dir = BACKUP_ROOT / safe_bot_name
        if not bot_backup_dir.exists():
            return []

        out: list[dict] = []
        for file_path in sorted(bot_backup_dir.rglob("*.zip")):
            try:
                stat = file_path.stat()
            except OSError:
                continue
            out.append(
                {
                    "name": file_path.name,
                    "size": stat.st_size,
                    "size_human": self._format_bytes(stat.st_size),
                    "mtime": stat.st_mtime,
                    "mtime_human": datetime.fromtimestamp(stat.st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
            )
        # Newest first.
        out.sort(key=lambda x: x["mtime"], reverse=True)
        return out

    def resolve_backup_path(self, bot_name: str, file_name: str) -> Path | None:
        """Safely resolve a backup zip path; rejects traversal attempts."""
        safe_bot_name = self._sanitize_name(bot_name)
        bot_backup_dir = (BACKUP_ROOT / safe_bot_name).resolve()
        if not bot_backup_dir.exists():
            return None
        candidate = (bot_backup_dir / file_name).resolve()
        # Reject anything that escaped the per-bot backup folder.
        try:
            candidate.relative_to(bot_backup_dir)
        except ValueError:
            return None
        if not candidate.is_file() or candidate.suffix.lower() != ".zip":
            return None
        return candidate

    # ------------------------------------------------------------------
    # Serialization for the web layer
    # ------------------------------------------------------------------

    def snapshot_bots(self) -> list[dict]:
        """Cheap-to-serialize view of every bot for the dashboard table."""
        with self.bots_lock:
            bots = list(self.bots.values())

        out: list[dict] = []
        for bot in sorted(bots, key=lambda b: b.name.lower()):
            health = self._get_backup_health(bot.name)
            storage_bytes = self._get_backup_storage_bytes(bot.name)
            health_metrics = self._collect_health_metrics(bot)
            out.append(
                {
                    "name": bot.name,
                    "path": str(bot.path),
                    "entry_file": bot.entry_file,
                    "is_running": bot.is_running,
                    "is_git_repo": bot.is_git_repo,
                    "update_available": bot.update_available,
                    "backup_health": health,
                    "backup_storage_bytes": storage_bytes,
                    "backup_storage_human": self._format_bytes(storage_bytes),
                    "last_backup_at": bot.last_backup_at,
                    "restart_on_crash": bot.restart_on_crash,
                    # Resource metrics (None when not running or psutil missing).
                    "pid": health_metrics["pid"],
                    "rss_bytes": health_metrics["rss_bytes"],
                    "rss_human": health_metrics["rss_human"],
                    "cpu_pct": health_metrics["cpu_pct"],
                    "uptime_sec": health_metrics["uptime_sec"],
                    "uptime_human": health_metrics["uptime_human"],
                }
            )
        return out

    def _collect_health_metrics(self, bot: BotInfo) -> dict:
        """Return PID/RSS/CPU/uptime for a running bot (Nones if unavailable).

        Cross-platform via psutil; works on Linux (LXC), macOS, and Windows.
        Tolerant of the process dying mid-snapshot or psutil being unavailable.
        """
        blank = {
            "pid": None,
            "rss_bytes": None,
            "rss_human": "",
            "cpu_pct": None,
            "uptime_sec": None,
            "uptime_human": "",
        }
        if not bot.is_running or bot.process is None:
            return blank
        pid = bot.process.pid
        if psutil is None:
            return {**blank, "pid": pid}
        proc = self._psutil_procs.get(bot.name)
        if proc is None or not proc.is_running() or proc.pid != pid:
            try:
                proc = psutil.Process(pid)
                proc.cpu_percent(interval=None)
                self._psutil_procs[bot.name] = proc
            except Exception:
                return {**blank, "pid": pid}
        try:
            rss = int(proc.memory_info().rss)
            cpu = float(proc.cpu_percent(interval=None))
            uptime = max(0.0, time.time() - proc.create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            self._psutil_procs.pop(bot.name, None)
            return {**blank, "pid": pid}
        return {
            "pid": pid,
            "rss_bytes": rss,
            "rss_human": self._format_bytes(rss),
            "cpu_pct": round(cpu, 1),
            "uptime_sec": uptime,
            "uptime_human": self._format_uptime(uptime),
        }

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m {s}s"
        h, m = divmod(m, 60)
        if h < 24:
            return f"{h}h {m}m"
        d, h = divmod(h, 24)
        return f"{d}d {h}h"

    def snapshot_backup_status(self) -> list[dict]:
        """Per-bot backup status for the 'Show Last Backup Times' view."""
        with self.bots_lock:
            bot_names = sorted(self.bots.keys())

        out: list[dict] = []
        for bot_name in bot_names:
            status = self.backup_status.get(bot_name, {})
            last_backup_at = float(status.get("last_backup_at", 0.0) or 0.0)
            last_failure_at = float(status.get("last_failure_at", 0.0) or 0.0)
            out.append(
                {
                    "name": bot_name,
                    "health": self._get_backup_health(bot_name),
                    "storage_bytes": self._get_backup_storage_bytes(bot_name),
                    "storage_human": self._format_bytes(self._get_backup_storage_bytes(bot_name)),
                    "last_backup_at": last_backup_at,
                    "last_backup_at_human": (
                        datetime.fromtimestamp(last_backup_at).strftime("%Y-%m-%d %H:%M:%S")
                        if last_backup_at > 0
                        else ""
                    ),
                    "last_failure_at": last_failure_at,
                    "last_failure_at_human": (
                        datetime.fromtimestamp(last_failure_at).strftime("%Y-%m-%d %H:%M:%S")
                        if last_failure_at > 0
                        else ""
                    ),
                    "last_error": str(status.get("last_error", "") or ""),
                    "last_backup_file": str(status.get("last_backup_file", "") or ""),
                    "files_count": int(status.get("files_count", 0) or 0),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Scheduler threads (replaces Tk's root.after chain)
    # ------------------------------------------------------------------

    def _reaper_loop(self) -> None:
        """Detect bot subprocesses that exited externally."""
        while not self._stop_event.is_set():
            with self.bots_lock:
                for bot in self.bots.values():
                    if bot.process is not None and bot.process.poll() is not None:
                        bot.process = None
                        bot.process_reader = None
                        self._psutil_procs.pop(bot.name, None)
            self._stop_event.wait(STATUS_REAPER_TICK_SEC)

    def _scheduler_loop(self) -> None:
        """Drive periodic update checks + backup checks."""
        while not self._stop_event.is_set():
            now = time.time()

            update_interval = max(60, int(self.config.update_interval_sec))
            if (
                not self._update_thread_running
                and now - self._last_global_update_check >= update_interval
            ):
                self._last_global_update_check = now
                threading.Thread(target=self._check_updates_worker, daemon=True).start()

            backup_interval_seconds = self.config.backup_interval_days * 86400
            if (
                not self._backup_thread_running
                and now - self._last_global_backup_check >= BACKUP_CHECK_INTERVAL_SEC
            ):
                self._last_global_backup_check = now
                threading.Thread(
                    target=self._periodic_backup_worker,
                    args=(backup_interval_seconds,),
                    daemon=True,
                ).start()

            self._stop_event.wait(SCHEDULER_TICK_SEC)
