import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

CONFIG_PATH = Path(__file__).with_name("manager_config.json")
ENTRY_CANDIDATES = ["main.py", "bot.py", "run.py", "app.py"]


@dataclass
class BotInfo:
    name: str
    path: Path
    entry_file: str
    is_git_repo: bool
    update_available: bool = False
    last_update_check: float = 0.0
    process: subprocess.Popen | None = None
    process_reader: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None


@dataclass
class AppConfig:
    bots_root: str = ""
    python_executable: str = ""
    update_interval_sec: int = 120
    auto_update_restart: bool = True

    @classmethod
    def load(cls) -> "AppConfig":
        if not CONFIG_PATH.exists():
            return cls()
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return cls(
                bots_root=str(data.get("bots_root", "")),
                python_executable=str(data.get("python_executable", "")),
                update_interval_sec=int(data.get("update_interval_sec", 120)),
                auto_update_restart=bool(data.get("auto_update_restart", True)),
            )
        except Exception:
            return cls()

    def save(self) -> None:
        payload = {
            "bots_root": self.bots_root,
            "python_executable": self.python_executable,
            "update_interval_sec": self.update_interval_sec,
            "auto_update_restart": self.auto_update_restart,
        }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class BotManagerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Discord Bot Manager")
        self.root.geometry("1100x700")
        self.root.minsize(900, 600)

        self.config = AppConfig.load()
        self.bots: dict[str, BotInfo] = {}
        self.bots_lock = threading.Lock()

        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.last_global_update_check = 0.0
        self.update_thread_running = False

        self.selected_bot_name: str | None = None

        self._build_ui()
        self._apply_theme()
        self._load_config_into_ui()
        self.scan_bots()

        self.root.after(500, self._drain_log_queue)
        self.root.after(1000, self._refresh_statuses)
        self.root.after(2000, self._periodic_update_loop)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=12)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Bots Root Folder:").pack(side=tk.LEFT)
        self.bots_root_var = tk.StringVar()
        self.bots_root_entry = ttk.Entry(top, textvariable=self.bots_root_var)
        self.bots_root_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))

        ttk.Button(top, text="Browse", command=self.pick_bots_root).pack(side=tk.LEFT)
        ttk.Button(top, text="Scan Bots", command=self.scan_bots).pack(side=tk.LEFT, padx=(8, 0))

        options = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        options.pack(fill=tk.X)

        ttk.Label(options, text="Python Executable (optional):").pack(side=tk.LEFT)
        self.python_var = tk.StringVar()
        self.python_entry = ttk.Entry(options, textvariable=self.python_var, width=40)
        self.python_entry.pack(side=tk.LEFT, padx=(8, 10))

        ttk.Label(options, text="Update Interval (sec):").pack(side=tk.LEFT)
        self.interval_var = tk.StringVar(value="120")
        self.interval_entry = ttk.Entry(options, textvariable=self.interval_var, width=8)
        self.interval_entry.pack(side=tk.LEFT, padx=(8, 12))

        self.auto_update_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options,
            text="Auto update + restart",
            variable=self.auto_update_var,
            command=self._save_config_from_ui,
        ).pack(side=tk.LEFT)

        main = ttk.Panedwindow(self.root, orient=tk.VERTICAL)
        main.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        upper = ttk.Frame(main)
        lower = ttk.Frame(main)
        main.add(upper, weight=3)
        main.add(lower, weight=2)

        # Pack the action bar FIRST at the bottom so it's never clipped
        # by the treeview when the upper pane is shrunk.
        actions = ttk.Frame(upper, padding=(0, 10, 0, 0))
        actions.pack(side=tk.BOTTOM, fill=tk.X)

        style = ttk.Style(self.root)
        style.configure("Toggle.TButton", padding=8, font=("Segoe UI", 10, "bold"))

        self.toggle_button = ttk.Button(
            actions,
            text="Start Bot",
            style="Toggle.TButton",
            command=self.toggle_selected,
        )
        self.toggle_button.pack(side=tk.LEFT)

        ttk.Button(actions, text="Restart", command=self.restart_selected).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Update Now", command=self.update_selected).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Check Updates", command=self.check_updates_now).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Save Settings", command=self._save_config_from_ui).pack(side=tk.RIGHT)

        table_wrap = ttk.Frame(upper)
        table_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        columns = ("status", "entry", "git", "update", "path")
        self.tree = ttk.Treeview(table_wrap, columns=columns, show="headings", height=12)
        self.tree.heading("status", text="Status")
        self.tree.heading("entry", text="Entry")
        self.tree.heading("git", text="Git")
        self.tree.heading("update", text="Update")
        self.tree.heading("path", text="Path")

        self.tree.column("status", width=90, anchor=tk.CENTER)
        self.tree.column("entry", width=120, anchor=tk.CENTER)
        self.tree.column("git", width=70, anchor=tk.CENTER)
        self.tree.column("update", width=120, anchor=tk.CENTER)
        self.tree.column("path", width=640, anchor=tk.W)

        yscroll = ttk.Scrollbar(table_wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        ttk.Label(lower, text="Logs").pack(anchor=tk.W)
        self.log_text = tk.Text(lower, wrap=tk.NONE, height=14)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.configure(state=tk.DISABLED)

    def _apply_theme(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        bg = "#f5f7fb"
        surface = "#ffffff"
        accent = "#1f6feb"
        text = "#14213d"

        style.configure("TFrame", background=bg)
        style.configure("TPanedwindow", background=bg)
        style.configure("TLabel", background=bg, foreground=text)
        style.configure("TButton", padding=6)
        style.configure("Treeview", rowheight=28, background=surface, fieldbackground=surface)
        style.configure("Treeview.Heading", padding=8)

        self.root.configure(background=bg)

        self.tree.tag_configure("running", background="#e9f5ec")
        self.tree.tag_configure("stopped", background="#fff7e9")
        self.tree.tag_configure("update", background="#ffecec")

        self.log_text.configure(background=surface, foreground="#0f172a", insertbackground=accent)

    def _load_config_into_ui(self) -> None:
        self.bots_root_var.set(self.config.bots_root)
        self.python_var.set(self.config.python_executable)
        self.interval_var.set(str(self.config.update_interval_sec))
        self.auto_update_var.set(self.config.auto_update_restart)

    def _save_config_from_ui(self) -> None:
        interval_raw = self.interval_var.get().strip() or "120"
        try:
            interval = max(15, int(interval_raw))
        except ValueError:
            interval = 120

        self.config.bots_root = self.bots_root_var.get().strip()
        self.config.python_executable = self.python_var.get().strip()
        self.config.update_interval_sec = interval
        self.config.auto_update_restart = bool(self.auto_update_var.get())
        self.config.save()
        self._append_log("SYSTEM", "Settings saved")

    def pick_bots_root(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.bots_root_var.get() or os.getcwd())
        if selected:
            self.bots_root_var.set(selected)
            self._save_config_from_ui()
            self.scan_bots()

    def scan_bots(self) -> None:
        self._save_config_from_ui()
        root_path = Path(self.bots_root_var.get().strip())
        if not root_path.exists() or not root_path.is_dir():
            self._append_log("SYSTEM", "Bots root folder is invalid")
            self._rebuild_tree()
            return

        discovered: dict[str, BotInfo] = {}

        candidates: list[Path] = []
        # Treat the root itself as a bot if it has an entry file (single-bot folder case).
        if self._detect_entry_file(root_path):
            candidates.append(root_path)
        # Plus any direct subfolders that look like bots.
        for child in sorted(root_path.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.name.startswith(".") and child.name != "__pycache__":
                candidates.append(child)

        for child in candidates:
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
                # Preserve process handle between scans.
                existing = self.bots[bot.name]
                bot.process = existing.process
                bot.process_reader = existing.process_reader
                bot.update_available = existing.update_available
                bot.last_update_check = existing.last_update_check

            discovered[bot.name] = bot

        with self.bots_lock:
            self.bots = discovered

        self._append_log("SYSTEM", f"Scan complete: found {len(discovered)} bot(s)")
        self._rebuild_tree()

    @staticmethod
    def _detect_entry_file(path: Path) -> str | None:
        for candidate in ENTRY_CANDIDATES:
            if (path / candidate).exists():
                return candidate
        return None

    def _rebuild_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        with self.bots_lock:
            for bot in sorted(self.bots.values(), key=lambda b: b.name.lower()):
                status = "Running" if bot.is_running else "Stopped"
                git_text = "Yes" if bot.is_git_repo else "No"
                upd = "Available" if bot.update_available else "Up-to-date"

                tag = "running" if bot.is_running else "stopped"
                if bot.update_available:
                    tag = "update"

                self.tree.insert(
                    "",
                    tk.END,
                    iid=bot.name,
                    values=(status, bot.entry_file, git_text, upd, str(bot.path)),
                    tags=(tag,),
                )

        if self.selected_bot_name and self.tree.exists(self.selected_bot_name):
            self.tree.selection_set(self.selected_bot_name)

        self._update_toggle_button()

    def _on_tree_select(self, _event: tk.Event) -> None:
        selected = self.tree.selection()
        if not selected:
            self.selected_bot_name = None
            self._update_toggle_button()
            return
        self.selected_bot_name = selected[0]
        self._update_toggle_button()

    def _update_toggle_button(self) -> None:
        if not self.selected_bot_name:
            self.toggle_button.configure(text="Start Bot", state=tk.DISABLED)
            return
        with self.bots_lock:
            bot = self.bots.get(self.selected_bot_name)
        if not bot:
            self.toggle_button.configure(text="Start Bot", state=tk.DISABLED)
            return
        self.toggle_button.configure(
            text="Stop Bot" if bot.is_running else "Start Bot",
            state=tk.NORMAL,
        )

    def _get_selected_bot(self) -> BotInfo | None:
        if not self.selected_bot_name:
            messagebox.showinfo("Select a bot", "Please select a bot first.")
            return None
        with self.bots_lock:
            return self.bots.get(self.selected_bot_name)

    def start_selected(self) -> None:
        bot = self._get_selected_bot()
        if not bot:
            return
        self._start_bot(bot)

    def stop_selected(self) -> None:
        bot = self._get_selected_bot()
        if not bot:
            return
        self._stop_bot(bot)

    def toggle_selected(self) -> None:
        bot = self._get_selected_bot()
        if not bot:
            return
        if bot.is_running:
            self._stop_bot(bot)
        else:
            self._start_bot(bot)
        self._update_toggle_button()

    def restart_selected(self) -> None:
        bot = self._get_selected_bot()
        if not bot:
            return
        self._restart_bot(bot)

    def update_selected(self) -> None:
        bot = self._get_selected_bot()
        if not bot:
            return
        threading.Thread(target=self._update_bot_worker, args=(bot.name,), daemon=True).start()

    def check_updates_now(self) -> None:
        if self.update_thread_running:
            return
        threading.Thread(target=self._check_updates_worker, daemon=True).start()

    def _python_command(self, bot: "BotInfo | None" = None) -> str:
        # 1. Per-bot venv wins (.venv or venv inside the bot folder).
        if bot is not None:
            for venv_dir in (".venv", "venv", "env"):
                candidate = bot.path / venv_dir / "Scripts" / "python.exe"
                if candidate.exists():
                    return str(candidate)
                # POSIX layout fallback
                candidate_posix = bot.path / venv_dir / "bin" / "python"
                if candidate_posix.exists():
                    return str(candidate_posix)

        # 2. User-configured python.
        custom = self.python_var.get().strip()
        if custom:
            return custom

        # 3. Fallback to the manager's interpreter.
        return sys.executable

    def _start_bot(self, bot: BotInfo) -> None:
        if bot.is_running:
            self._append_log(bot.name, "Already running")
            return

        command = [self._python_command(bot), bot.entry_file]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(bot.path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            self._append_log(bot.name, f"Failed to start: {exc}")
            return

        bot.process = process
        reader = threading.Thread(target=self._read_process_output, args=(bot.name, process), daemon=True)
        bot.process_reader = reader
        reader.start()

        self._append_log(bot.name, f"Started ({' '.join(command)})")
        self._rebuild_tree()

    def _stop_bot(self, bot: BotInfo) -> None:
        if not bot.is_running:
            self._append_log(bot.name, "Already stopped")
            return

        assert bot.process is not None
        bot.process.terminate()
        try:
            bot.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bot.process.kill()

        bot.process = None
        bot.process_reader = None
        self._append_log(bot.name, "Stopped")
        self._rebuild_tree()

    def _restart_bot(self, bot: BotInfo) -> None:
        self._append_log(bot.name, "Restarting")
        if bot.is_running:
            self._stop_bot(bot)
        self._start_bot(bot)

    def _read_process_output(self, bot_name: str, process: subprocess.Popen) -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                self.log_queue.put((bot_name, line.rstrip()))
        except Exception as exc:
            self.log_queue.put((bot_name, f"Log stream error: {exc}"))
        finally:
            self.log_queue.put((bot_name, "Process exited"))

    def _append_log(self, source: str, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] [{source}] {message}\n"
        self.log_queue.put(("_ui_", line))

    def _drain_log_queue(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        wrote = False
        while True:
            try:
                source, payload = self.log_queue.get_nowait()
            except queue.Empty:
                break

            if source == "_ui_":
                self.log_text.insert(tk.END, payload)
            else:
                self.log_text.insert(tk.END, f"[{source}] {payload}\n")
            wrote = True

        if wrote:
            self.log_text.see(tk.END)

        self.log_text.configure(state=tk.DISABLED)
        self.root.after(500, self._drain_log_queue)

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

    def _check_updates_worker(self) -> None:
        self.update_thread_running = True
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
                    self._append_log(bot.name, "Update detected on origin/main")
                    if self.auto_update_var.get():
                        self._update_bot_worker(bot.name, silent=True)
                else:
                    self._append_log(bot.name, "No update")

            self.root.after(0, self._rebuild_tree)
        finally:
            self.update_thread_running = False

    def _has_remote_update(self, bot: BotInfo) -> bool:
        ok, out = self._run_git(bot.path, ["fetch", "origin", "main"])
        if not ok:
            self._append_log(bot.name, f"Update check failed: {out}")
            return False

        ok, out = self._run_git(bot.path, ["rev-list", "--count", "HEAD..origin/main"])
        if not ok:
            self._append_log(bot.name, f"Unable to compare HEAD with origin/main: {out}")
            return False

        try:
            count = int(out.strip().splitlines()[-1])
            return count > 0
        except Exception:
            self._append_log(bot.name, f"Unexpected git output: {out}")
            return False

    def _update_bot_worker(self, bot_name: str, silent: bool = False) -> None:
        with self.bots_lock:
            bot = self.bots.get(bot_name)
        if not bot:
            return
        if not bot.is_git_repo:
            if not silent:
                self._append_log(bot.name, "Not a git repo; cannot update")
            return

        was_running = bot.is_running

        ok, output = self._run_git(bot.path, ["pull", "origin", "main", "--ff-only"], timeout=90)
        if not ok:
            self._append_log(bot.name, f"Update failed: {output}")
            return

        bot.update_available = False
        self._append_log(bot.name, "Updated from origin/main")

        if self.auto_update_var.get() and was_running:
            self._append_log(bot.name, "Restarting after update")
            self._restart_bot(bot)

        self.root.after(0, self._rebuild_tree)

    def _refresh_statuses(self) -> None:
        changed = False
        with self.bots_lock:
            for bot in self.bots.values():
                if bot.process is not None and bot.process.poll() is not None:
                    bot.process = None
                    bot.process_reader = None
                    changed = True

        if changed:
            self._rebuild_tree()

        self.root.after(1000, self._refresh_statuses)

    def _periodic_update_loop(self) -> None:
        interval = max(15, int(self.interval_var.get() or "120"))
        now = time.time()
        if not self.update_thread_running and now - self.last_global_update_check >= interval:
            self.last_global_update_check = now
            threading.Thread(target=self._check_updates_worker, daemon=True).start()

        self.root.after(3000, self._periodic_update_loop)

    def _on_close(self) -> None:
        running_bots: list[BotInfo] = []
        with self.bots_lock:
            for bot in self.bots.values():
                if bot.is_running:
                    running_bots.append(bot)

        if running_bots:
            names = ", ".join(bot.name for bot in running_bots)
            answer = messagebox.askyesno(
                "Stop running bots?",
                f"The following bots are still running:\n{names}\n\nStop them and exit?",
            )
            if not answer:
                return

            for bot in running_bots:
                self._stop_bot(bot)

        self._save_config_from_ui()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    BotManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
