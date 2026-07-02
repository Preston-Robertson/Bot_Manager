"""FastAPI adapter over `manager_core.BotManager`.

Endpoints mirror the old Tkinter buttons 1:1. Long/blocking operations
(git, backups) are dispatched to a threadpool so they never block the
event loop.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from manager_core import BotManager, LogEntry, UPLOAD_FILE_MAX_BYTES

logger = logging.getLogger("bot_manager.web")

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

AUTH_TOKEN = os.environ.get("BOTMGR_TOKEN", "").strip()


# ---------------------------------------------------------------------------
# App + lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the engine on startup, shut it down cleanly on exit.
    manager = BotManager()
    manager.start()
    app.state.manager = manager
    if not AUTH_TOKEN:
        logger.warning(
            "BOTMGR_TOKEN not set — the dashboard is unauthenticated. "
            "Only expose this on a trusted LAN."
        )
    try:
        yield
    finally:
        manager.shutdown()


app = FastAPI(title="Discord Bot Manager", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def get_manager(request: Request) -> BotManager:
    return request.app.state.manager


# ---------------------------------------------------------------------------
# Auth (optional shared-secret token)
# ---------------------------------------------------------------------------


def _extract_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header_token = request.headers.get("x-bot-token", "").strip()
    if header_token:
        return header_token
    return request.query_params.get("token", "").strip()


def require_auth(request: Request) -> None:
    if not AUTH_TOKEN:
        return
    # Constant-time comparison to defeat response-timing token discovery.
    if not hmac.compare_digest(_extract_token(request), AUTH_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing token"
        )


AuthDep = Depends(require_auth)


# ---------------------------------------------------------------------------
# Dashboard page
# ---------------------------------------------------------------------------


@app.get("/")
async def dashboard(request: Request):
    # The HTML page itself is unprotected so the login form (if we add one)
    # can render. The auth check happens on every /api/* call.
    return templates.TemplateResponse(
        request,
        "index.html",
        {"auth_required": bool(AUTH_TOKEN)},
    )


# ---------------------------------------------------------------------------
# API: bots
# ---------------------------------------------------------------------------


@app.get("/api/bots", dependencies=[AuthDep])
async def list_bots(request: Request):
    manager = get_manager(request)
    return {"ok": True, "bots": manager.snapshot_bots()}


@app.post("/api/bots/scan", dependencies=[AuthDep])
async def scan_bots(request: Request):
    manager = get_manager(request)
    await run_in_threadpool(manager.scan_bots)
    return {"ok": True, "bots": manager.snapshot_bots()}


def _require_bot(manager: BotManager, name: str) -> None:
    with manager.bots_lock:
        if name not in manager.bots:
            raise HTTPException(status_code=404, detail=f"Unknown bot: {name}")


@app.post("/api/bots/{name}/start", dependencies=[AuthDep])
async def start_bot(name: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    ok, msg = await run_in_threadpool(manager.start_bot, name)
    return {"ok": ok, "message": msg}


@app.post("/api/bots/{name}/stop", dependencies=[AuthDep])
async def stop_bot(name: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    ok, msg = await run_in_threadpool(manager.stop_bot, name)
    return {"ok": ok, "message": msg}


@app.post("/api/bots/{name}/restart", dependencies=[AuthDep])
async def restart_bot(name: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    ok, msg = await run_in_threadpool(manager.restart_bot, name)
    return {"ok": ok, "message": msg}


@app.post("/api/bots/{name}/update", dependencies=[AuthDep])
async def update_bot(name: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    ok, msg = await run_in_threadpool(manager.update_bot, name)
    return {"ok": ok, "message": msg}


@app.post("/api/updates/check", dependencies=[AuthDep])
async def check_updates(request: Request):
    manager = get_manager(request)
    # Fire-and-forget; the worker thread updates state in-place.
    await run_in_threadpool(manager.check_updates)
    return {"ok": True, "message": "update check started"}


# ---------------------------------------------------------------------------
# API: manager self-update
# ---------------------------------------------------------------------------


@app.get("/api/manager/info", dependencies=[AuthDep])
async def manager_info(request: Request):
    manager = get_manager(request)
    return {"ok": True, "manager": manager.manager_status()}


@app.post("/api/manager/check-update", dependencies=[AuthDep])
async def manager_check_update(request: Request):
    manager = get_manager(request)
    info = await run_in_threadpool(manager.check_manager_update)
    return {"ok": True, "manager": info}


@app.post("/api/manager/update", dependencies=[AuthDep])
async def manager_update(request: Request):
    manager = get_manager(request)
    force = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            force = bool(body.get("force", False))
    except Exception:
        # No body / not JSON — treat as a normal (non-force) update.
        pass
    ok, msg = await run_in_threadpool(manager.update_manager, force=force)
    info = manager.manager_status()
    if not ok:
        return JSONResponse(
            {"ok": False, "message": msg, "manager": info}, status_code=400
        )
    return {"ok": True, "message": msg, "manager": info}


@app.post("/api/manager/restart", dependencies=[AuthDep])
async def manager_restart(request: Request):
    manager = get_manager(request)
    ok, msg = manager.restart_manager()
    return {"ok": ok, "message": msg}


@app.post("/api/bots/add-from-git", dependencies=[AuthDep])
async def add_bot_from_git(request: Request):
    manager = get_manager(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    repo_url = str(payload.get("repo_url") or "").strip()
    branch = str(payload.get("branch") or "").strip() or None
    install_deps = bool(payload.get("install_deps", True))

    if not repo_url:
        raise HTTPException(status_code=400, detail="repo_url is required")

    # Clone + venv + pip install can be slow; run off the event loop.
    ok, msg = await run_in_threadpool(
        manager.add_bot_from_git, repo_url, branch, install_deps
    )
    if not ok:
        return JSONResponse(
            {"ok": False, "message": msg}, status_code=400
        )
    return {"ok": True, "message": msg, "bots": manager.snapshot_bots()}


# ---------------------------------------------------------------------------
# API: per-bot config files (in-browser editor)
# ---------------------------------------------------------------------------


@app.get("/api/bots/{name}/files", dependencies=[AuthDep])
async def list_bot_files(name: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    files = manager.list_config_files(name)
    if files is None:
        raise HTTPException(status_code=404, detail=f"Unknown bot: {name}")
    return {"ok": True, "files": files}


@app.get("/api/bots/{name}/files/{file_path:path}", dependencies=[AuthDep])
async def read_bot_file(name: str, file_path: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    ok, payload = manager.read_config_file(name, file_path)
    if not ok:
        return JSONResponse({"ok": False, "message": payload}, status_code=400)
    return {"ok": True, "path": file_path, "content": payload}


@app.put("/api/bots/{name}/files/{file_path:path}", dependencies=[AuthDep])
async def write_bot_file(name: str, file_path: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    payload = await request.json()
    if not isinstance(payload, dict) or "content" not in payload:
        raise HTTPException(
            status_code=400, detail="Body must include a 'content' field"
        )
    content = payload.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="'content' must be a string")
    ok, msg = manager.write_config_file(name, file_path, content)
    if not ok:
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    return {"ok": True, "message": msg}


@app.post("/api/bots/{name}/folders", dependencies=[AuthDep])
async def create_bot_folder(name: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    payload = await request.json()
    if not isinstance(payload, dict) or "path" not in payload:
        raise HTTPException(
            status_code=400, detail="Body must include a 'path' field"
        )
    rel_path = payload.get("path")
    if not isinstance(rel_path, str):
        raise HTTPException(status_code=400, detail="'path' must be a string")
    ok, msg = manager.create_bot_folder(name, rel_path)
    if not ok:
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    return {"ok": True, "message": msg, "path": rel_path}


@app.delete("/api/bots/{name}/folders/{folder_path:path}", dependencies=[AuthDep])
async def delete_bot_folder(
    name: str,
    folder_path: str,
    request: Request,
    recursive: bool = Query(False),
):
    """Delete a folder inside a bot directory.

    Non-recursive by default (empty folders only). Pass `?recursive=true`
    to force-remove a non-empty tree — the UI does this after an extra
    confirmation prompt.
    """
    manager = get_manager(request)
    _require_bot(manager, name)
    ok, msg = manager.delete_bot_folder(name, folder_path, recursive=recursive)
    if not ok:
        if msg == "Folder not found":
            status_code = 404
        elif msg == "Folder is not empty":
            # 409 lets the client distinguish "needs recursive" from other
            # failures without string-matching the message.
            status_code = 409
        else:
            status_code = 400
        return JSONResponse(
            {"ok": False, "message": msg, "path": folder_path},
            status_code=status_code,
        )
    return {"ok": True, "message": msg, "path": folder_path}


@app.post("/api/bots/{name}/uploads/{file_path:path}", dependencies=[AuthDep])
async def upload_bot_file(name: str, file_path: str, request: Request):
    """Accept a raw binary body and persist it under the bot folder.

    The request body is the file bytes directly (not multipart). Frontend
    sends `fetch(url, { method: 'POST', body: fileObject })`. Size is
    enforced both via Content-Length (when present) and after reading.
    """
    manager = get_manager(request)
    _require_bot(manager, name)
    # Short-circuit on Content-Length when the client advertises it, so we
    # don't buffer a giant upload just to reject it.
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > UPLOAD_FILE_MAX_BYTES:
                return JSONResponse(
                    {
                        "ok": False,
                        "message": (
                            f"Upload too large; limit {UPLOAD_FILE_MAX_BYTES} bytes"
                        ),
                    },
                    status_code=413,
                )
        except ValueError:
            pass
    data = await request.body()
    if len(data) > UPLOAD_FILE_MAX_BYTES:
        return JSONResponse(
            {
                "ok": False,
                "message": f"Upload too large; limit {UPLOAD_FILE_MAX_BYTES} bytes",
            },
            status_code=413,
        )
    if not data:
        return JSONResponse(
            {"ok": False, "message": "Empty upload"}, status_code=400
        )
    ok, msg = manager.upload_bot_file(name, file_path, data)
    if not ok:
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    return {"ok": True, "message": msg, "path": file_path, "size": len(data)}


@app.delete("/api/bots/{name}/files/{file_path:path}", dependencies=[AuthDep])
async def delete_bot_file(name: str, file_path: str, request: Request):
    """Delete an editable or uploaded file inside the bot folder."""
    manager = get_manager(request)
    _require_bot(manager, name)
    ok, msg = manager.delete_bot_file(name, file_path)
    if not ok:
        # Map "not found" to 404 for clarity; other failures stay 400.
        status_code = 404 if msg == "File not found" else 400
        return JSONResponse({"ok": False, "message": msg}, status_code=status_code)
    return {"ok": True, "message": msg}


# Separate prefix so it doesn't collide with the JSON `GET .../files/{path}`
# editor read route. Returns the file bytes as a download attachment.
@app.get(
    "/api/bots/{name}/files-download/{file_path:path}",
    dependencies=[AuthDep],
)
async def download_bot_file(name: str, file_path: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    path = manager._resolve_bot_any_path(name, file_path)
    if path is None:
        raise HTTPException(status_code=400, detail="File not allowed")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/octet-stream",
    )


@app.put("/api/bots/{name}/settings", dependencies=[AuthDep])
async def set_bot_settings(name: str, request: Request):
    """Update per-bot settings (currently only `restart_on_crash: bool`)."""
    manager = get_manager(request)
    _require_bot(manager, name)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    restart_on_crash = payload.get("restart_on_crash")
    if restart_on_crash is not None and not isinstance(restart_on_crash, bool):
        raise HTTPException(
            status_code=400, detail="'restart_on_crash' must be a boolean"
        )
    ok, msg = manager.set_bot_settings(name, restart_on_crash=restart_on_crash)
    if not ok:
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    return {"ok": True, "message": msg, "bots": manager.snapshot_bots()}


# ---------------------------------------------------------------------------
# API: backups
# ---------------------------------------------------------------------------


@app.post("/api/bots/{name}/backup", dependencies=[AuthDep])
async def backup_bot(name: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    ok, msg = manager.backup_bot(name, reason="manual")
    return {"ok": ok, "message": msg}


@app.post("/api/backups/all", dependencies=[AuthDep])
async def backup_all(request: Request):
    manager = get_manager(request)
    count = manager.backup_all(reason="manual")
    return {"ok": True, "message": f"queued backup for {count} bot(s)"}


@app.get("/api/backups/status", dependencies=[AuthDep])
async def backup_status_view(request: Request):
    manager = get_manager(request)
    return {"ok": True, "status": manager.snapshot_backup_status()}


@app.get("/api/bots/{name}/backups", dependencies=[AuthDep])
async def list_bot_backups(name: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    return {"ok": True, "backups": manager.list_backups(name)}


@app.get("/api/bots/{name}/backups/{file_name}/download", dependencies=[AuthDep])
async def download_backup(name: str, file_name: str, request: Request):
    manager = get_manager(request)
    _require_bot(manager, name)
    path = manager.resolve_backup_path(name, file_name)
    if path is None:
        raise HTTPException(status_code=404, detail="Backup file not found")
    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/zip",
    )


# ---------------------------------------------------------------------------
# API: config
# ---------------------------------------------------------------------------


@app.get("/api/config", dependencies=[AuthDep])
async def get_config(request: Request):
    manager = get_manager(request)
    cfg = manager.config
    return {
        "ok": True,
        "config": {
            "bots_root": cfg.bots_root,
            "python_executable": cfg.python_executable,
            "update_interval_sec": cfg.update_interval_sec,
            "backup_interval_days": cfg.backup_interval_days,
            "auto_update_restart": cfg.auto_update_restart,
        },
    }


@app.post("/api/config", dependencies=[AuthDep])
async def set_config(request: Request):
    manager = get_manager(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    allowed = {
        "bots_root",
        "python_executable",
        "update_interval_sec",
        "backup_interval_days",
        "auto_update_restart",
    }
    fields = {k: v for k, v in payload.items() if k in allowed}
    cfg = manager.update_config(**fields)
    return {
        "ok": True,
        "config": {
            "bots_root": cfg.bots_root,
            "python_executable": cfg.python_executable,
            "update_interval_sec": cfg.update_interval_sec,
            "backup_interval_days": cfg.backup_interval_days,
            "auto_update_restart": cfg.auto_update_restart,
        },
    }


# ---------------------------------------------------------------------------
# WebSocket: live logs
# ---------------------------------------------------------------------------


@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    # Auth manually because Depends() isn't applied to WebSocket routes.
    if AUTH_TOKEN:
        token = (
            websocket.query_params.get("token", "").strip()
            or websocket.headers.get("x-bot-token", "").strip()
        )
        if not hmac.compare_digest(token, AUTH_TOKEN):
            await websocket.close(code=4401)
            return

    await websocket.accept()
    manager: BotManager = websocket.app.state.manager
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[LogEntry] = asyncio.Queue(maxsize=1000)

    def on_log(entry: LogEntry) -> None:
        # Called from worker threads — hop back to the event loop.
        try:
            loop.call_soon_threadsafe(_safe_put, queue, entry)
        except RuntimeError:
            # Loop may be closing during shutdown.
            pass

    manager.subscribe_logs(on_log)

    try:
        # Send recent backlog first so a fresh page has context.
        for entry in manager.recent_logs():
            await websocket.send_json(entry.to_dict())

        while True:
            entry = await queue.get()
            await websocket.send_json(entry.to_dict())
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Log websocket error: %s", exc)
    finally:
        manager.unsubscribe_logs(on_log)


def _safe_put(queue: asyncio.Queue, entry: LogEntry) -> None:
    # Drop oldest if a slow client is backing up so we never block the engine.
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(entry)


# ---------------------------------------------------------------------------
# Health check (no auth — handy for systemd / curl)
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})
