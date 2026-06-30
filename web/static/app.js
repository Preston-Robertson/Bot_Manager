"use strict";

const AUTH_REQUIRED = !!window.__BOTMGR__?.authRequired;
const TOKEN_KEY = "botmgr.token";
const THEME_KEY = "botmgr.theme";
const VALID_THEMES = ["light", "dark", "solarized"];

const state = {
  selected: null,
  bots: [],
  ws: null,
  reconnectTimer: null,
};

// ---------------------------------------------------------------------------
// Token + fetch helpers
// ---------------------------------------------------------------------------

function getToken() {
  return AUTH_REQUIRED ? (localStorage.getItem(TOKEN_KEY) || "") : "";
}

function setToken(value) {
  if (value) localStorage.setItem(TOKEN_KEY, value);
  else localStorage.removeItem(TOKEN_KEY);
}

function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

function getTheme() {
  const saved = localStorage.getItem(THEME_KEY) || "light";
  return VALID_THEMES.includes(saved) ? saved : "light";
}

function applyTheme(theme) {
  if (!VALID_THEMES.includes(theme)) theme = "light";
  if (theme === "light") {
    // Light is the default; an attribute-less root keeps the :root cascade clean.
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", theme);
  }
  localStorage.setItem(THEME_KEY, theme);
}

async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...authHeaders(),
    ...(options.headers || {}),
  };
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || body.message || detail;
    } catch {}
    throw new Error(`${res.status}: ${detail}`);
  }
  // Some endpoints return empty bodies; tolerate that.
  const text = await res.text();
  return text ? JSON.parse(text) : {};
}

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

let toastEl = null;
function toast(msg, kind = "info") {
  if (!toastEl) {
    toastEl = document.createElement("div");
    toastEl.className = "toast";
    document.body.appendChild(toastEl);
  }
  toastEl.textContent = msg;
  toastEl.className = `toast show ${kind === "error" ? "error" : ""}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toastEl.className = "toast"; }, 3000);
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

async function loadConfig() {
  try {
    const { config } = await api("/api/config");
    document.getElementById("botsRoot").value = config.bots_root || "";
    document.getElementById("pythonExe").value = config.python_executable || "";
    document.getElementById("updateInterval").value = config.update_interval_sec ?? 86400;
    document.getElementById("backupInterval").value = config.backup_interval_days ?? 1;
    document.getElementById("autoUpdate").checked = !!config.auto_update_restart;
  } catch (e) {
    toast(`Load config failed: ${e.message}`, "error");
  }
}

async function saveConfig() {
  const payload = {
    bots_root: document.getElementById("botsRoot").value.trim(),
    python_executable: document.getElementById("pythonExe").value.trim(),
    update_interval_sec: parseInt(document.getElementById("updateInterval").value || "86400", 10),
    backup_interval_days: parseInt(document.getElementById("backupInterval").value || "1", 10),
    auto_update_restart: document.getElementById("autoUpdate").checked,
  };
  try {
    await api("/api/config", { method: "POST", body: JSON.stringify(payload) });
    toast("Settings saved");
  } catch (e) {
    toast(`Save failed: ${e.message}`, "error");
  }
}

// ---------------------------------------------------------------------------
// Bots table
// ---------------------------------------------------------------------------

function statusBadge(running) {
  return `<span class="badge ${running ? "Running" : "Stopped"}">${running ? "Running" : "Stopped"}</span>`;
}

function updateBadge(available) {
  return available
    ? `<span class="badge Available">Available</span>`
    : `<span class="badge UpToDate">Up-to-date</span>`;
}

function healthBadge(health) {
  // CSS class names with spaces use the escaped form in CSS but plain in HTML.
  return `<span class="badge ${health}">${health}</span>`;
}

function renderBots() {
  const tbody = document.getElementById("botsTbody");
  if (!state.bots.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty">No bots found. Set a root folder and click Scan Bots.</td></tr>`;
    return;
  }

  tbody.innerHTML = state.bots.map((bot) => {
    let rowClass = bot.is_running ? "running" : "stopped";
    if (bot.update_available) rowClass = "update";
    if (bot.name === state.selected) rowClass += " selected";

    return `
      <tr class="${rowClass}" data-name="${escapeHtml(bot.name)}">
        <td>${escapeHtml(bot.name)}</td>
        <td>${statusBadge(bot.is_running)}</td>
        <td>${escapeHtml(bot.entry_file)}</td>
        <td>${bot.is_git_repo ? "Yes" : "No"}</td>
        <td>${updateBadge(bot.update_available)}</td>
        <td>${healthCellHtml(bot)}</td>
        <td>${healthBadge(bot.backup_health)}</td>
        <td>${escapeHtml(bot.backup_storage_human)}</td>
        <td class="path">${escapeHtml(bot.path)}</td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", () => {
      state.selected = tr.dataset.name;
      renderBots();
      updateActionBar();
    });
  });
}

// Compact "PID 1234 · CPU 1.2% · RSS 142 MB · 1h 23m" cell.
function healthCellHtml(bot) {
  if (!bot.is_running) return `<span class="muted">—</span>`;
  const parts = [];
  if (bot.pid != null) parts.push(`PID ${bot.pid}`);
  if (bot.cpu_pct != null) parts.push(`CPU ${bot.cpu_pct.toFixed(1)}%`);
  if (bot.rss_human) parts.push(`RSS ${escapeHtml(bot.rss_human)}`);
  if (bot.uptime_human) parts.push(escapeHtml(bot.uptime_human));
  return parts.length ? `<span class="health">${parts.join(" &middot; ")}</span>` : `<span class="muted">running</span>`;
}

function updateActionBar() {
  const bot = state.bots.find((b) => b.name === state.selected);
  const toggleBtn = document.getElementById("toggleBtn");
  const restartBtn = document.getElementById("restartBtn");
  const updateBtn = document.getElementById("updateBtn");
  const backupBtn = document.getElementById("backupBtn");
  const downloadsBtn = document.getElementById("downloadsBtn");
  const configBtn = document.getElementById("configBtn");
  const label = document.getElementById("selectedLabel");
  const crashWrap = document.getElementById("restartOnCrashWrap");
  const crashChk = document.getElementById("restartOnCrash");

  if (!bot) {
    toggleBtn.textContent = "Start Bot";
    toggleBtn.disabled = true;
    restartBtn.disabled = true;
    updateBtn.disabled = true;
    backupBtn.disabled = true;
    downloadsBtn.disabled = true;
    configBtn.disabled = true;
    label.textContent = "No bot selected";
    crashWrap.style.display = "none";
    crashChk.disabled = true;
    return;
  }

  toggleBtn.textContent = bot.is_running ? "Stop Bot" : "Start Bot";
  toggleBtn.disabled = false;
  restartBtn.disabled = false;
  updateBtn.disabled = !bot.is_git_repo;
  backupBtn.disabled = false;
  downloadsBtn.disabled = false;
  configBtn.disabled = false;
  label.textContent = `Selected: ${bot.name}`;
  crashWrap.style.display = "";
  crashChk.disabled = false;
  // Don't fire the change handler while we sync state from the server.
  crashChk.dataset.syncing = "1";
  crashChk.checked = !!bot.restart_on_crash;
  delete crashChk.dataset.syncing;
}

async function refreshBots() {
  try {
    const { bots } = await api("/api/bots");
    state.bots = bots;
    renderBots();
    updateActionBar();
  } catch (e) {
    // Don't spam toasts on poll failures; surface once via the conn badge.
    setConn(false, e.message);
  }
}

// ---------------------------------------------------------------------------
// Bot actions
// ---------------------------------------------------------------------------

async function callBotAction(action, message) {
  if (!state.selected) { toast("Select a bot first", "error"); return; }
  try {
    await api(`/api/bots/${encodeURIComponent(state.selected)}/${action}`, { method: "POST" });
    toast(message);
    refreshBots();
  } catch (e) {
    toast(`${message} failed: ${e.message}`, "error");
  }
}

async function toggleSelected() {
  const bot = state.bots.find((b) => b.name === state.selected);
  if (!bot) return;
  await callBotAction(bot.is_running ? "stop" : "start", bot.is_running ? "Stop requested" : "Start requested");
}

async function checkUpdates() {
  try {
    await api("/api/updates/check", { method: "POST" });
    toast("Update check started");
  } catch (e) {
    toast(`Check failed: ${e.message}`, "error");
  }
}
// ---------------------------------------------------------------------------
// Manager self-update
// ---------------------------------------------------------------------------

function renderManagerInfo(info) {
  const badge = document.getElementById("mgrStatusBadge");
  const dirty = document.getElementById("mgrDirtyBadge");
  const branch = document.getElementById("mgrBranch");
  const commit = document.getElementById("mgrCommit");
  const text = document.getElementById("mgrUpdateText");
  const lastCheck = document.getElementById("mgrLastCheck");
  const updateBtn = document.getElementById("mgrUpdateBtn");  const forceBtn = document.getElementById("mgrForceUpdateBtn");

  // Default: hide force button; renderers below decide when to show it.
  forceBtn.classList.add("hidden");
  if (!info || !info.is_git_repo) {
    badge.textContent = "not a git checkout";
    badge.className = "badge badge-neutral";
    branch.textContent = "—";
    commit.textContent = "—";
    text.textContent = "Self-update is only available when the manager is run from a git clone.";
    lastCheck.textContent = "";
    dirty.classList.add("hidden");
    updateBtn.disabled = true;
    return;
  }

  branch.textContent = info.branch || "(detached)";
  commit.textContent = info.head_short || "—";

  if (info.dirty) dirty.classList.remove("hidden");
  else dirty.classList.add("hidden");

  if (info.update_available) {
    badge.textContent = `${info.behind} commit(s) behind`;
    badge.className = "badge badge-update";
    if (info.dirty) {
      const files = Array.isArray(info.dirty_files) ? info.dirty_files : [];
      const preview = files.length
        ? ` Dirty: ${files.slice(0, 5).join(", ")}${files.length > 5 ? `, +${files.length - 5} more` : ""}.`
        : "";
      text.textContent =
        `An update is available on origin/${info.branch}, but the manager working tree has uncommitted changes.${preview} ` +
        `Use "Force Update" to stash local changes and pull, or clean the tree on the host and re-check.`;
      forceBtn.classList.remove("hidden");
    } else {
      text.textContent = `An update is available on origin/${info.branch}. Click "Update Manager" to pull, then "Restart Manager" to apply.`;
    }
    updateBtn.disabled = !!info.dirty;
  } else {
    badge.textContent = "up to date";
    badge.className = "badge badge-ok";
    text.textContent = info.ahead > 0
      ? `Local is ${info.ahead} commit(s) ahead of origin/${info.branch}.`
      : `Tracking origin/${info.branch}.`;
    updateBtn.disabled = true;
  }

  if (info.last_check && info.last_check > 0) {
    const d = new Date(info.last_check * 1000);
    lastCheck.textContent = `last checked ${d.toLocaleTimeString()}`;
  } else {
    lastCheck.textContent = "never checked";
  }
}

async function loadManagerInfo() {
  try {
    const { manager } = await api("/api/manager/info");
    renderManagerInfo(manager);
  } catch (err) {
    console.warn("loadManagerInfo failed:", err);
  }
}

async function mgrCheck() {
  const badge = document.getElementById("mgrStatusBadge");
  badge.textContent = "checking…";
  badge.className = "badge badge-neutral";
  try {
    const { manager } = await api("/api/manager/check-update", { method: "POST" });
    renderManagerInfo(manager);
    toast(manager.update_available
      ? `Manager update available (${manager.behind} behind)`
      : "Manager is up to date");
  } catch (err) {
    toast(`Manager check failed: ${err.message}`, "error");
  }
}

async function mgrUpdate() {
  if (!confirm("Pull the latest manager code from origin? You'll need to restart afterwards to apply.")) return;
  try {
    const res = await api("/api/manager/update", { method: "POST" });
    renderManagerInfo(res.manager);
    toast(res.message || "Manager updated");
  } catch (err) {
    toast(`Update failed: ${err.message}`, "error");
  }
}

async function mgrForceUpdate() {
  if (!confirm(
    "Force update will stash any local changes in the manager working tree, then pull. " +
    "Your local edits are preserved in `git stash` on the host (recover with `git stash list` / `git stash pop`). " +
    "Continue?"
  )) return;
  try {
    const res = await api("/api/manager/update", {
      method: "POST",
      body: JSON.stringify({ force: true }),
    });
    renderManagerInfo(res.manager);
    toast(res.message || "Manager updated (forced)");
  } catch (err) {
    toast(`Force update failed: ${err.message}`, "error");
  }
}

async function mgrRestart() {
  if (!confirm("Restart the manager now? Running bots will be stopped first, then re-discovered on boot.")) return;
  try {
    const res = await api("/api/manager/restart", { method: "POST" });
    toast(res.message || "Restart scheduled");
    // The server is about to exec; show a clear waiting state.
    document.getElementById("mgrStatusBadge").textContent = "restarting…";
    document.getElementById("mgrStatusBadge").className = "badge badge-warn";
    // Give it a few seconds, then poll until it answers again.
    setTimeout(pollUntilBack, 3000);
  } catch (err) {
    toast(`Restart failed: ${err.message}`, "error");
  }
}

async function pollUntilBack(attempt = 0) {
  if (attempt > 30) {
    toast("Manager did not come back within 60s — check the server", "error");
    return;
  }
  try {
    await fetch("/healthz", { cache: "no-store" });
    toast("Manager is back online");
    await loadManagerInfo();
    await loadConfig();
    await refreshBots();
    connectLogs();
  } catch {
    setTimeout(() => pollUntilBack(attempt + 1), 2000);
  }
}
async function scanBots() {
  try {
    await api("/api/bots/scan", { method: "POST" });
    toast("Scan complete");
    refreshBots();
  } catch (e) {
    toast(`Scan failed: ${e.message}`, "error");
  }
}

async function backupAll() {
  try {
    const res = await api("/api/backups/all", { method: "POST" });
    toast(res.message || "Backup queued");
  } catch (e) {
    toast(`Backup all failed: ${e.message}`, "error");
  }
}

async function addBotFromGit() {
  const urlEl = document.getElementById("addRepoUrl");
  const branchEl = document.getElementById("addRepoBranch");
  const installEl = document.getElementById("addRepoInstall");
  const btn = document.getElementById("addRepoBtn");

  const repo_url = urlEl.value.trim();
  if (!repo_url) { toast("Enter a repository URL", "error"); return; }

  const payload = {
    repo_url,
    branch: branchEl.value.trim(),
    install_deps: installEl.checked,
  };

  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = "Cloning…";
  toast("Cloning repo — watch the log panel for progress");
  try {
    const res = await api("/api/bots/add-from-git", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    toast(res.message || "Bot added");
    urlEl.value = "";
    branchEl.value = "";
    refreshBots();
  } catch (e) {
    toast(`Add bot failed: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

// ---------------------------------------------------------------------------
// Modal (backup status + backup file list)
// ---------------------------------------------------------------------------

function openModal(title, html) {
  document.getElementById("modalTitle").textContent = title;
  document.getElementById("modalContent").innerHTML = html;
  document.getElementById("modal").classList.remove("hidden");
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
}

async function showBackupStatus() {
  try {
    const { status } = await api("/api/backups/status");
    if (!status.length) {
      openModal("Backup Status", `<p>No bots discovered yet.</p>`);
      return;
    }
    const rows = status.map((s) => `
      <tr>
        <td>${escapeHtml(s.name)}</td>
        <td>${healthBadge(s.health)}</td>
        <td>${escapeHtml(s.storage_human)}</td>
        <td>${escapeHtml(s.last_backup_at_human || "—")}</td>
        <td>${s.files_count}</td>
        <td>${escapeHtml(s.last_error || "")}</td>
      </tr>
    `).join("");
    openModal("Backup Status", `
      <table>
        <thead><tr><th>Bot</th><th>Health</th><th>Size</th><th>Last Success</th><th>Files</th><th>Last Error</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `);
  } catch (e) {
    toast(`Status failed: ${e.message}`, "error");
  }
}

async function showBackupFiles() {
  if (!state.selected) { toast("Select a bot first", "error"); return; }
  const name = state.selected;
  try {
    const { backups } = await api(`/api/bots/${encodeURIComponent(name)}/backups`);
    if (!backups.length) {
      openModal(`Backups — ${name}`, `<p>No backup archives for this bot yet.</p>`);
      return;
    }
    const token = getToken();
    const tokenQs = token ? `?token=${encodeURIComponent(token)}` : "";
    const rows = backups.map((b) => `
      <tr>
        <td><a href="/api/bots/${encodeURIComponent(name)}/backups/${encodeURIComponent(b.name)}/download${tokenQs}" download>${escapeHtml(b.name)}</a></td>
        <td>${escapeHtml(b.size_human)}</td>
        <td>${escapeHtml(b.mtime_human)}</td>
      </tr>
    `).join("");
    openModal(`Backups — ${name}`, `
      <table>
        <thead><tr><th>File</th><th>Size</th><th>Modified</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `);
  } catch (e) {
    toast(`List backups failed: ${e.message}`, "error");
  }
}

// ---------------------------------------------------------------------------
// Per-bot config-file editor
// ---------------------------------------------------------------------------

async function showConfigFiles() {
  if (!state.selected) { toast("Select a bot first", "error"); return; }
  const name = state.selected;
  try {
    const { files } = await api(`/api/bots/${encodeURIComponent(name)}/files`);
    const rows = files.length
      ? files.map((f) => {
          // Uploaded binaries are listed but can't open in the text editor.
          const cell = f.editable === false
            ? `<span class="file-binary" title="Binary file — not editable">${escapeHtml(f.path)}</span> <span class="badge badge-neutral">binary</span>`
            : `<a href="#" class="file-link" data-path="${escapeHtml(f.path)}">${escapeHtml(f.path)}</a>`;
          const actions = `
            <button type="button" class="small file-dl-btn" data-path="${escapeHtml(f.path)}" title="Download">&darr;</button>
            <button type="button" class="small danger file-rm-btn" data-path="${escapeHtml(f.path)}" title="Delete">&times;</button>
          `;
          return `
            <tr>
              <td>${cell}</td>
              <td>${escapeHtml(f.size_human)}</td>
              <td>${escapeHtml(f.mtime_human)}</td>
              <td class="file-actions">${actions}</td>
            </tr>
          `;
        }).join("")
      : `<tr><td colspan="4" class="empty">No editable config files found yet. Use the inputs below to create one.</td></tr>`;

    openModal(`Config — ${name}`, `
      <table>
        <thead><tr><th>File</th><th>Size</th><th>Modified</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="create-file-row">
        <input id="newConfigName" type="text" placeholder="config.json or local_data/notes.txt" />
        <button id="newConfigBtn" type="button">Create / Open File</button>
      </div>
      <div class="create-file-row">
        <input id="newFolderName" type="text" placeholder="local_data" />
        <button id="newFolderBtn" type="button">Create Folder</button>
      </div>
      <div class="create-file-row">
        <input id="uploadDest" type="text" placeholder="models/luigi.pkl (target path inside bot)" />
        <input id="uploadFile" type="file" />
        <button id="uploadBtn" type="button">Upload Binary</button>
      </div>
      <p class="hint">Text editor extensions: .json .env .yaml .yml .toml .ini .cfg .txt (or any <code>.env*</code>). Max 1 MB per file. Subfolders supported (use <code>/</code> in the filename).</p>
      <p class="hint">Upload extensions (binary, not editable): .pkl .pickle .csv .db .db3 .sqlite .sqlite3 .xls .xlsm .xlsx. Max 25 MB per file. <strong>Note:</strong> pickle files are unsafe to load from untrusted sources — only upload pickles you trust.</p>
      <p class="hint">Max depth 5, max 500 files. Excluded: <code>.git .venv venv env __pycache__ node_modules</code>.</p>
    `);

    document.querySelectorAll("#modal .file-link").forEach((a) => {
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        editConfigFile(name, ev.target.dataset.path);
      });
    });
    document.querySelectorAll("#modal .file-dl-btn").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        downloadBotFile(name, ev.currentTarget.dataset.path);
      });
    });
    document.querySelectorAll("#modal .file-rm-btn").forEach((btn) => {
      btn.addEventListener("click", async (ev) => {
        ev.preventDefault();
        const fpath = ev.currentTarget.dataset.path;
        if (!confirm(`Delete "${fpath}"?\nThis cannot be undone.`)) return;
        try {
          await api(
            `/api/bots/${encodeURIComponent(name)}/files/${encodeFilePath(fpath)}`,
            { method: "DELETE" }
          );
          toast(`Deleted: ${fpath}`);
          showConfigFiles();
        } catch (e) {
          toast(`Delete failed: ${e.message}`, "error");
        }
      });
    });
    document.getElementById("newConfigBtn").addEventListener("click", () => {
      const fname = document.getElementById("newConfigName").value.trim();
      if (!fname) { toast("Enter a filename or path", "error"); return; }
      editConfigFile(name, fname, /*createIfMissing=*/true);
    });
    document.getElementById("newFolderBtn").addEventListener("click", async () => {
      const folder = document.getElementById("newFolderName").value.trim();
      if (!folder) { toast("Enter a folder name", "error"); return; }
      try {
        await api(`/api/bots/${encodeURIComponent(name)}/folders`, {
          method: "POST",
          body: JSON.stringify({ path: folder }),
        });
        toast(`Folder created: ${folder}`);
        showConfigFiles();
      } catch (e) {
        toast(`Create folder failed: ${e.message}`, "error");
      }
    });
    document.getElementById("uploadBtn").addEventListener("click", async () => {
      const fileInput = document.getElementById("uploadFile");
      const destInput = document.getElementById("uploadDest");
      const file = fileInput.files && fileInput.files[0];
      if (!file) { toast("Pick a file to upload", "error"); return; }
      const dest = destInput.value.trim() || file.name;
      try {
        await uploadBinaryFile(name, dest, file);
        toast(`Uploaded: ${dest}`);
        showConfigFiles();
      } catch (e) {
        toast(`Upload failed: ${e.message}`, "error");
      }
    });
  } catch (e) {
    toast(`Load config files failed: ${e.message}`, "error");
  }
}

// Send a binary file as the raw request body (no multipart, no JSON wrap).
// We bypass the `api()` helper because it forces a JSON Content-Type.
async function uploadBinaryFile(botName, destPath, file) {
  const url = `/api/bots/${encodeURIComponent(botName)}/uploads/${encodeFilePath(destPath)}`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      ...authHeaders(),
      "Content-Type": file.type || "application/octet-stream",
    },
    body: file,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || body.message || detail;
    } catch {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

// Fetch a file with auth headers and trigger a browser download. We can't
// use a plain <a download> link because the route requires Bearer auth.
async function downloadBotFile(botName, filePath) {
  const url = `/api/bots/${encodeURIComponent(botName)}/files-download/${encodeFilePath(filePath)}`;
  try {
    const res = await fetch(url, { headers: authHeaders() });
    if (!res.ok) {
      let detail = res.statusText;
      try { const body = await res.json(); detail = body.detail || body.message || detail; } catch {}
      throw new Error(`${res.status}: ${detail}`);
    }
    const blob = await res.blob();
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl;
    // Browser uses the last path segment as the suggested filename.
    a.download = filePath.split("/").pop();
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(objUrl), 5000);
  } catch (e) {
    toast(`Download failed: ${e.message}`, "error");
  }
}

// Encode each path segment but leave forward slashes intact so the FastAPI
// `{file_path:path}` converter receives the multi-segment path as-is.
function encodeFilePath(p) {
  return p.split("/").map(encodeURIComponent).join("/");
}

async function editConfigFile(botName, filePath, createIfMissing = false) {
  let content = "";
  try {
    const res = await api(
      `/api/bots/${encodeURIComponent(botName)}/files/${encodeFilePath(filePath)}`
    );
    content = res.content || "";
  } catch (e) {
    if (!createIfMissing) {
      toast(`Open failed: ${e.message}`, "error");
      return;
    }
    // File doesn't exist yet — start with an empty buffer.
    content = "";
  }

  openModal(`Edit ${filePath} — ${botName}`, `
    <textarea id="configEditor" class="editor-textarea" spellcheck="false"></textarea>
    <div class="config-toolbar">
      <button id="backToListBtn" type="button">Back to list</button>
      <div>
        <button id="saveConfigBtn" type="button" class="primary">Save</button>
      </div>
    </div>
    <p class="hint">After saving, restart the bot to pick up the change. Parent folders will be created automatically on save.</p>
  `);

  document.getElementById("configEditor").value = content;
  document.getElementById("backToListBtn").addEventListener("click", () => showConfigFiles());
  document.getElementById("saveConfigBtn").addEventListener("click", async () => {
    const newContent = document.getElementById("configEditor").value;
    try {
      await api(
        `/api/bots/${encodeURIComponent(botName)}/files/${encodeFilePath(filePath)}`,
        { method: "PUT", body: JSON.stringify({ content: newContent }) }
      );
      toast(`Saved ${filePath} — restart the bot to apply`);
    } catch (e) {
      toast(`Save failed: ${e.message}`, "error");
    }
  });
}

// ---------------------------------------------------------------------------
// WebSocket log stream
// ---------------------------------------------------------------------------

const LOG_MAX_LINES = 1000;
const logsEl = document.getElementById("logs");

function appendLog(entry) {
  const line = `[${entry.time}] [${entry.source}] ${entry.message}\n`;
  logsEl.appendChild(document.createTextNode(line));
  // Trim oldest if we're over the cap.
  while (logsEl.childNodes.length > LOG_MAX_LINES) {
    logsEl.removeChild(logsEl.firstChild);
  }
  logsEl.scrollTop = logsEl.scrollHeight;
}

function setConn(connected, msg) {
  const el = document.getElementById("connStatus");
  el.className = `conn ${connected ? "dot-running" : "dot-stopped"}`;
  el.textContent = connected ? "connected" : (msg ? `disconnected: ${msg}` : "disconnected");
}

function connectLogs() {
  if (state.ws) try { state.ws.close(); } catch {}
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const token = getToken();
  const qs = token ? `?token=${encodeURIComponent(token)}` : "";
  const ws = new WebSocket(`${proto}//${location.host}/ws/logs${qs}`);
  state.ws = ws;

  ws.addEventListener("open", () => setConn(true));
  ws.addEventListener("message", (ev) => {
    try { appendLog(JSON.parse(ev.data)); } catch {}
  });
  ws.addEventListener("close", () => {
    setConn(false);
    // Auto-reconnect after a short delay.
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(connectLogs, 3000);
  });
  ws.addEventListener("error", () => { /* close handler will reconnect */ });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function bindEvents() {
  document.getElementById("saveSettingsBtn").addEventListener("click", saveConfig);
  document.getElementById("scanBtn").addEventListener("click", scanBots);
  document.getElementById("toggleBtn").addEventListener("click", toggleSelected);
  document.getElementById("restartBtn").addEventListener("click", () => callBotAction("restart", "Restart requested"));
  document.getElementById("updateBtn").addEventListener("click", () => callBotAction("update", "Update requested"));
  document.getElementById("checkUpdatesBtn").addEventListener("click", checkUpdates);
  document.getElementById("backupBtn").addEventListener("click", () => callBotAction("backup", "Backup queued"));
  document.getElementById("backupAllBtn").addEventListener("click", backupAll);
  document.getElementById("statusBtn").addEventListener("click", showBackupStatus);
  document.getElementById("downloadsBtn").addEventListener("click", showBackupFiles);
  document.getElementById("configBtn").addEventListener("click", showConfigFiles);
  document.getElementById("addRepoBtn").addEventListener("click", addBotFromGit);
  document.getElementById("mgrCheckBtn").addEventListener("click", mgrCheck);

  // Auto-restart-on-crash toggle for the selected bot.
  document.getElementById("restartOnCrash").addEventListener("change", async (ev) => {
    if (ev.target.dataset.syncing) return; // change came from a server-state refresh
    if (!state.selected) return;
    const want = !!ev.target.checked;
    try {
      const { bots } = await api(
        `/api/bots/${encodeURIComponent(state.selected)}/settings`,
        { method: "PUT", body: JSON.stringify({ restart_on_crash: want }) }
      );
      if (bots) { state.bots = bots; renderBots(); updateActionBar(); }
      toast(`Auto-restart ${want ? "enabled" : "disabled"} for ${state.selected}`);
    } catch (e) {
      toast(`Update failed: ${e.message}`, "error");
      // Roll the checkbox back to whatever the server most recently said.
      updateActionBar();
    }
  });

  const themeSelect = document.getElementById("themeSelect");
  if (themeSelect) {
    themeSelect.value = getTheme();
    themeSelect.addEventListener("change", (ev) => {
      applyTheme(ev.target.value);
    });
  }
  document.getElementById("mgrUpdateBtn").addEventListener("click", mgrUpdate);
  document.getElementById("mgrForceUpdateBtn").addEventListener("click", mgrForceUpdate);
  document.getElementById("mgrRestartBtn").addEventListener("click", mgrRestart);
  document.getElementById("clearLogsBtn").addEventListener("click", () => { logsEl.textContent = ""; });
  document.getElementById("modalCloseBtn").addEventListener("click", closeModal);
  document.getElementById("modal").addEventListener("click", (ev) => {
    if (ev.target.id === "modal") closeModal();
  });

  const tokenInput = document.getElementById("tokenInput");
  const tokenSave = document.getElementById("tokenSaveBtn");
  if (tokenInput && tokenSave) {
    tokenInput.value = getToken();
    tokenSave.addEventListener("click", () => {
      setToken(tokenInput.value.trim());
      toast("Token saved — reconnecting");
      loadConfig();
      refreshBots();
      connectLogs();
    });
  }
}

async function init() {
  bindEvents();
  await loadConfig();
  await refreshBots();
  await loadManagerInfo();
  connectLogs();
  setInterval(refreshBots, 2500);
  setInterval(loadManagerInfo, 30000);
}

document.addEventListener("DOMContentLoaded", init);
