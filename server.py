#!/usr/bin/env python3
"""
server.py — Meeting Notes UI backend

Start: python server.py  (or ./run.sh --server)
Opens: http://localhost:8000
"""

import asyncio
import json
import os
import re
import shutil
import signal as _signal
import subprocess
import sys
import time
import uuid
import urllib.request as _urllib
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

import config
from config import (
    ACTIONS_FILE, AGENT_STATUS_FILE, OUTPUT_DIR,
    SLACK_WEBHOOK_URL, CUSTOM_AGENTS_FILE,
    NOTION_TOKEN, NOTION_DATABASE_ID,
)
from handlers import calendar as cal_handler
from handlers import email as email_handler
from handlers import notion as notion_handler
from llm import LLMError, generate_text, provider_status

app = FastAPI(title="Meeting Notes")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
LOCAL_ORIGIN_RE = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=LOCAL_ORIGIN_RE,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


def _is_local_origin(value: str | None) -> bool:
    if not value:
        return True
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = parsed.hostname or ""
    return parsed.scheme in {"http", "https"} and host in LOCAL_HOSTS


@app.middleware("http")
async def _protect_local_write_endpoints(request: Request, call_next):
    if request.method in {"POST", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if not _is_local_origin(origin):
            return JSONResponse({"error": "blocked cross-origin localhost write"}, status_code=403)
        if not origin and referer and not _is_local_origin(referer):
            return JSONResponse({"error": "blocked cross-origin localhost write"}, status_code=403)
    return await call_next(request)

UI_DIR          = Path(__file__).parent / "ui"
UI_FILE         = UI_DIR / "index.html"
PID_FILE        = Path("/tmp/meetingnotes.pid")
DISPATCH_FILE   = Path("/tmp/meetingnotes.dispatch")
ARCHIVE_DIR     = Path.home() / "Desktop" / "meeting-archive"
SESSION_META_FILE = OUTPUT_DIR / "session-meta.json"

_meetingenv     = Path(__file__).parent / "meetingenv" / "bin" / "python3"
PYTHON_BIN      = str(_meetingenv) if _meetingenv.exists() else sys.executable
MEETINGNOTES_PY = Path(__file__).parent / "meetingnotes.py"

FILES_MAP = {
    "transcript": OUTPUT_DIR / "transcription.md",
    "notes":      OUTPUT_DIR / "meeting-notes.md",
    "qa":         OUTPUT_DIR / "interview-answers.md",
    "sketch":     OUTPUT_DIR / "sketch.md",
}

# ── Process state ──────────────────────────────────────────────────────────────
_proc:            subprocess.Popen | None = None
_proc_start_time: float | None           = None
_paused:          bool                   = False

# ── Config file helpers ────────────────────────────────────────────────────────
_CONFIG_FILE = Path.home() / ".config" / "meetingnotes" / "config.json"

def _read_config() -> dict:
    try:
        if _CONFIG_FILE.exists():
            return json.loads(_CONFIG_FILE.read_text())
    except Exception:
        pass
    return {}

def _write_config(data: dict) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    merged = _read_config()
    merged.update(data)
    _CONFIG_FILE.write_text(json.dumps(merged))


def _normalize_meeting_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())[:80]


def _read_current_session_name() -> str:
    try:
        if SESSION_META_FILE.exists():
            data = json.loads(SESSION_META_FILE.read_text())
            return _normalize_meeting_name(data.get("name", ""))
    except Exception:
        pass
    return ""


def _write_current_session_name(name: str | None) -> str:
    clean = _normalize_meeting_name(name)
    if not clean:
        SESSION_META_FILE.unlink(missing_ok=True)
        return ""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_META_FILE.write_text(json.dumps({
        "name": clean,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }))
    return clean


async def _force_kill_after(pid: int, delay: int = 90) -> None:
    """Send SIGKILL to pid after delay seconds if it's still alive."""
    await asyncio.sleep(delay)
    try:
        os.kill(pid, _signal.SIGKILL)
        print(f"[server] Force-killed PID {pid} after {delay}s timeout", flush=True)
    except Exception:
        pass


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(
        content=UI_FILE.read_text(),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/ui/{filename}")
async def ui_static(filename: str):
    path = UI_DIR / filename
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path)


# ── Control ────────────────────────────────────────────────────────────────────

@app.get("/control/status")
async def control_status():
    global _proc, _proc_start_time
    running = bool(_proc and _proc.poll() is None)
    if not running and PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            running = True
        except Exception:
            PID_FILE.unlink(missing_ok=True)
    elapsed = int(time.time() - _proc_start_time) if (_proc_start_time and running) else 0
    return {"running": running, "elapsed": elapsed, "paused": _paused, "name": _read_current_session_name()}


@app.get("/control/session-title")
async def get_session_title(infer: bool = False):
    name = _read_current_session_name()
    inferred = False
    if not name and infer:
        name = _normalize_meeting_name(_infer_meeting_name())
        if name:
            _write_current_session_name(name)
            inferred = True
    return {"name": name, "inferred": inferred}


@app.patch("/control/session-title")
async def set_session_title(request: Request):
    body = await request.json()
    name = _write_current_session_name(body.get("name", ""))
    return {"ok": True, "name": name}


def _infer_meeting_name() -> str:
    """Extract a name from notes file, using LLM when content is available."""
    notes_file = FILES_MAP.get("notes")
    if not notes_file or not notes_file.exists():
        return ""
    text = notes_file.read_text().strip()
    if not text or len(text) < 50:
        return ""
    # Try LLM for a concise title
    try:
        snippet = text[:800]
        prompt = (
            "Generate a concise meeting title (5–7 words max, title case, no quotes) "
            "based on these meeting notes:\n\n" + snippet +
            "\n\nOutput ONLY the title text, nothing else."
        )
        title = generate_text(prompt, timeout=20).strip().strip('"').strip("'")
        # Sanity check: not too long, not empty, no newlines
        if title and len(title) < 80 and "\n" not in title:
            return title[:60]
    except Exception:
        pass
    # Fallback: first H1/H2 heading
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "): return s[2:].strip()[:60]
        if s.startswith("## ") and "meeting" not in s.lower(): return s[3:].strip()[:60]
    return ""


def _archive_session(meeting_name: str = "") -> dict | None:
    """Copy current output files to ARCHIVE_DIR. Returns archive metadata or None if empty."""
    has_content = any(p.exists() and p.stat().st_size > 4 for p in FILES_MAP.values())
    if not has_content:
        return None
    name = _normalize_meeting_name(meeting_name) or _read_current_session_name() or _infer_meeting_name()
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    slug = re.sub(r"[^\w\-]", "_", name)[:40] if name else ""
    meeting_name = name  # use inferred name in meta
    session_id = f"{ts}_{slug}" if slug else ts
    dest = ARCHIVE_DIR / session_id
    dest.mkdir(parents=True, exist_ok=True)
    for path in FILES_MAP.values():
        if path.exists():
            shutil.copy2(path, dest / path.name)
    if ACTIONS_FILE.exists():
        shutil.copy2(ACTIONS_FILE, dest / ACTIONS_FILE.name)
    action_count = 0
    if ACTIONS_FILE.exists():
        try:
            action_count = len(json.loads(ACTIONS_FILE.read_text()))
        except Exception:
            pass
    (dest / "meta.json").write_text(json.dumps({
        "name": meeting_name, "timestamp": ts, "id": session_id, "actions": action_count
    }))
    if meeting_name:
        _write_current_session_name(meeting_name)
    return {"id": session_id, "name": meeting_name}


@app.post("/control/new-session")
async def control_new_session(request: Request):
    global _proc, _proc_start_time, _paused
    _paused = False
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    meeting_name = body.get("name", "").strip()

    # Auto-archive before clearing
    archived = _archive_session(meeting_name)

    # Stop any running process
    if _proc and _proc.poll() is None:
        _proc.send_signal(_signal.SIGINT)
        _proc = None
        _proc_start_time = None
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, _signal.SIGINT)
        except Exception:
            pass
        PID_FILE.unlink(missing_ok=True)
    DISPATCH_FILE.unlink(missing_ok=True)
    # Clear all output files
    for path in FILES_MAP.values():
        if path.exists():
            path.unlink()
    if ACTIONS_FILE.exists():
        ACTIONS_FILE.unlink()
    AGENT_STATUS_FILE.unlink(missing_ok=True)
    SESSION_META_FILE.unlink(missing_ok=True)
    for ca in _load_custom_agents():
        (OUTPUT_DIR / f"custom-{ca['id']}.md").unlink(missing_ok=True)
    return {"ok": True, "archived": archived["id"] if archived else None, "name": archived["name"] if archived else ""}


@app.post("/control/start")
async def control_start(request: Request):
    global _proc, _proc_start_time
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    meeting_name = _normalize_meeting_name(body.get("name", ""))
    # Check in-process handle first
    if _proc and _proc.poll() is None:
        return JSONResponse({"ok": False, "error": "already running"}, status_code=409)
    # Also check PID file — catches a meetingnotes.py started by a previous server instance
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)   # raises if process is gone
            return JSONResponse({"ok": False, "error": "already running (pid {})".format(pid)}, status_code=409)
        except (ProcessLookupError, PermissionError):
            PID_FILE.unlink(missing_ok=True)  # stale — clean up
        except Exception:
            pass
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Clear stale output files so UI shows fresh content
    for path in FILES_MAP.values():
        if path.exists():
            path.unlink()
    if ACTIONS_FILE.exists():
        ACTIONS_FILE.unlink()
    AGENT_STATUS_FILE.unlink(missing_ok=True)
    _write_current_session_name(meeting_name)
    for ca in _load_custom_agents():
        (OUTPUT_DIR / f"custom-{ca['id']}.md").unlink(missing_ok=True)
    log_file = open(OUTPUT_DIR / "session.log", "w")
    lang = _read_config().get("language", "en")
    cmd  = [PYTHON_BIN, str(MEETINGNOTES_PY)]
    if lang and lang != "en":
        cmd += ["--language", lang]
    _proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=log_file,
        stderr=log_file,
        text=True,
        cwd=str(Path(__file__).parent),
    )
    _proc_start_time = time.time()
    return {"ok": True, "pid": _proc.pid}


@app.post("/control/stop")
async def control_stop(request: Request):
    global _proc, _paused
    _paused = False
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    meeting_name = body.get("name", "").strip()

    # Archive before stopping so files are still present
    archived = _archive_session(meeting_name)
    archived_id = archived["id"] if archived else None
    archived_name = archived["name"] if archived else _read_current_session_name()

    if _proc and _proc.poll() is None:
        pid = _proc.pid
        _proc.send_signal(_signal.SIGINT)
        asyncio.create_task(_force_kill_after(pid, delay=90))
        return {"ok": True, "archived": archived_id}
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, _signal.SIGINT)
            asyncio.create_task(_force_kill_after(pid, delay=90))
            return {"ok": True, "archived": archived_id, "name": archived_name}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    return JSONResponse({"ok": False, "error": "not running"}, status_code=404)


@app.post("/control/dispatch")
async def control_dispatch():
    # Check something is actually running first
    status = await control_status()
    if not status["running"]:
        return JSONResponse({"ok": False, "error": "not running"}, status_code=404)
    try:
        DISPATCH_FILE.touch()
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/control/pause")
async def control_pause():
    global _paused
    pid = None
    if _proc and _proc.poll() is None:
        pid = _proc.pid
    elif PID_FILE.exists():
        try: pid = int(PID_FILE.read_text().strip())
        except Exception: pass
    if pid:
        try:
            os.kill(pid, _signal.SIGSTOP)
            _paused = True
            return {"ok": True, "paused": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": False, "error": "not running"}, status_code=404)


@app.post("/control/resume")
async def control_resume():
    global _paused
    pid = None
    if _proc and _proc.poll() is None:
        pid = _proc.pid
    elif PID_FILE.exists():
        try: pid = int(PID_FILE.read_text().strip())
        except Exception: pass
    if pid:
        try:
            os.kill(pid, _signal.SIGCONT)
            _paused = False
            return {"ok": True, "paused": False}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": False, "error": "not running"}, status_code=404)


@app.post("/control/dispatch-agent")
async def dispatch_single_agent(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    tx_file = FILES_MAP.get("transcript")
    if not tx_file or not tx_file.exists():
        return JSONResponse({"error": "no transcript yet"}, status_code=404)

    def _run():
        subprocess.run(
            [PYTHON_BIN, str(MEETINGNOTES_PY), "--dispatch-only", "--agent", name],
            cwd=str(Path(__file__).parent),
            timeout=120,
        )

    asyncio.get_event_loop().run_in_executor(None, _run)
    return {"ok": True}




def _note_actions_from_notes(notes_text: str) -> list[dict]:
    items: list[dict] = []
    in_actions = False
    for line in notes_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("## action items"):
            in_actions = True
            continue
        if in_actions and stripped.startswith("## "):
            break
        if not in_actions or not stripped.startswith("- [ ]"):
            continue
        desc = stripped[5:].strip()
        owner = ""
        for sep in (" — ", " - "):
            if sep in desc:
                left, right = desc.rsplit(sep, 1)
                if right.strip():
                    desc, owner = left.strip(), right.strip()
                break
        if desc and "none" not in desc.lower():
            items.append({
                "id": f"n{len(items) + 1}",
                "type": "notion",
                "description": desc,
                "owner": owner or "me",
                "deadline": "",
                "priority": "medium",
                "context": "Fallback action parsed from meeting notes.",
                "status": "pending",
            })
    return items


def _load_actions_with_fallback() -> list:
    actions: list = []
    if ACTIONS_FILE.exists():
        try:
            actions = json.loads(ACTIONS_FILE.read_text() or "[]")
        except Exception:
            actions = []
    if actions:
        return actions
    notes_file = FILES_MAP.get("notes")
    if notes_file and notes_file.exists():
        return _note_actions_from_notes(notes_file.read_text())
    return []


def _read_agent_status() -> dict:
    if AGENT_STATUS_FILE.exists():
        try:
            return json.loads(AGENT_STATUS_FILE.read_text())
        except Exception:
            pass
    status = provider_status()
    return {"provider": status.get("provider"), "running": False, "agents": {}, "health": status}

# ── Multiplexed SSE — ONE connection for all panels ───────────────────────────
# Replaces the 5-7 individual SSE streams that used to hit the browser's
# HTTP/1.1 per-origin connection limit (6), causing fetch() requests to queue.

async def _stream_all_events() -> AsyncGenerator[str, None]:
    last: dict[str, str | None] = {name: None for name in FILES_MAP}
    last["actions"] = None
    last["agent_status"] = None
    custom_last: dict[str, str | None] = {}

    while True:
        # Static files (transcript, notes, qa, sketch)
        for name, path in FILES_MAP.items():
            try:
                if path.exists():
                    content = path.read_text()
                    if content != last[name]:
                        last[name] = content
                        yield f"data: {json.dumps({'type': name, 'content': content})}\n\n"
            except Exception:
                pass

        # Actions file — skip empty/partial reads to avoid wiping the UI mid-write
        try:
            if ACTIONS_FILE.exists():
                raw = ACTIONS_FILE.read_text()
                if raw != last["actions"] and raw.strip():
                    try:
                        data = json.loads(raw)
                        if not data:
                            data = _load_actions_with_fallback()
                        last["actions"] = raw
                        yield f"data: {json.dumps({'type': 'actions', 'data': data})}\n\n"
                    except json.JSONDecodeError:
                        pass  # skip partial file write
        except Exception:
            pass


        # Agent status / AI health
        try:
            status_raw = AGENT_STATUS_FILE.read_text() if AGENT_STATUS_FILE.exists() else json.dumps(_read_agent_status())
            if status_raw != last["agent_status"]:
                last["agent_status"] = status_raw
                yield f"data: {json.dumps({'type': 'agent-status', 'data': json.loads(status_raw)})}\n\n"
        except Exception:
            pass

        # Custom agent files — picked up automatically as agents are added/removed
        for ca in _load_custom_agents():
            aid  = ca["id"]
            path = OUTPUT_DIR / f"custom-{aid}.md"
            try:
                if path.exists():
                    content = path.read_text()
                    if content != custom_last.get(aid):
                        custom_last[aid] = content
                        yield f"data: {json.dumps({'type': f'custom-{aid}', 'content': content})}\n\n"
            except Exception:
                pass

        await asyncio.sleep(0.5)


@app.get("/events")
async def events_stream():
    return StreamingResponse(
        _stream_all_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── File REST (kept for boot-time fetch) ──────────────────────────────────────

@app.get("/files/{name}")
async def files_get(name: str):
    path = FILES_MAP.get(name)
    if not path or not path.exists():
        return JSONResponse({"content": ""})
    return JSONResponse({"content": path.read_text()})


# ── Custom agents ─────────────────────────────────────────────────────────────

def _load_custom_agents() -> list:
    try:
        if CUSTOM_AGENTS_FILE.exists():
            return json.loads(CUSTOM_AGENTS_FILE.read_text())
    except Exception:
        pass
    return []

def _save_custom_agents(agents: list) -> None:
    CUSTOM_AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM_AGENTS_FILE.write_text(json.dumps(agents, indent=2))

@app.get("/agents/custom")
async def get_custom_agents():
    return JSONResponse(_load_custom_agents())

@app.post("/agents/custom")
async def create_custom_agent(request: Request):
    body   = await request.json()
    name   = body.get("name", "").strip()
    prompt = body.get("prompt", "").strip()
    if not name or not prompt:
        return JSONResponse({"error": "name and prompt are required"}, status_code=400)
    if len(_load_custom_agents()) >= 8:
        return JSONResponse({"error": "Maximum 8 custom agents reached"}, status_code=400)
    agent = {
        "id":         f"ca_{uuid.uuid4().hex[:8]}",
        "name":       name,
        "prompt":     prompt,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    agents = _load_custom_agents()
    agents.append(agent)
    _save_custom_agents(agents)
    return JSONResponse(agent)

@app.delete("/agents/custom/{agent_id}")
async def delete_custom_agent(agent_id: str):
    agents = [a for a in _load_custom_agents() if a["id"] != agent_id]
    _save_custom_agents(agents)
    (OUTPUT_DIR / f"custom-{agent_id}.md").unlink(missing_ok=True)
    return {"ok": True}

@app.get("/files/custom/{agent_id}")
async def get_custom_agent_file(agent_id: str):
    path = OUTPUT_DIR / f"custom-{agent_id}.md"
    return JSONResponse({"content": path.read_text() if path.exists() else ""})


# ── Actions REST ───────────────────────────────────────────────────────────────

@app.get("/actions")
async def get_actions():
    return JSONResponse(_load_actions_with_fallback())




@app.post("/actions/reopen")
async def reopen_action(request: Request):
    payload   = await request.json()
    action_id = payload.get("id")
    if not ACTIONS_FILE.exists():
        return JSONResponse({"error": "no actions file"}, status_code=404)
    try:
        actions = json.loads(ACTIONS_FILE.read_text())
        for a in actions:
            if a.get("id") == action_id:
                a["status"] = "pending"
        ACTIONS_FILE.write_text(json.dumps(actions, indent=2))
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/actions/done")
async def mark_done(request: Request):
    payload   = await request.json()
    action_id = payload.get("id")
    if not ACTIONS_FILE.exists():
        return JSONResponse({"error": "no actions file"}, status_code=404)
    try:
        actions = json.loads(ACTIONS_FILE.read_text())
        for a in actions:
            if a.get("id") == action_id:
                a["status"] = "done"
        ACTIONS_FILE.write_text(json.dumps(actions, indent=2))
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Act: draft ─────────────────────────────────────────────────────────────────

@app.post("/act/draft")
async def draft_action(request: Request):
    try:
        action = await request.json()
        t = action.get("type")
        if t == "email":
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, email_handler.draft, action)
        if t == "calendar":
            return cal_handler.draft(action)
        if t == "notion":
            return notion_handler.draft(action)
        if t == "research":
            return await _research_draft(action)
        return JSONResponse({"error": f"unknown type: {t}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Act: execute ───────────────────────────────────────────────────────────────

@app.post("/act/execute")
async def execute_action(request: Request):
    try:
        payload = await request.json()
        t    = payload.get("type")
        data = payload.get("data", {})
        if t == "calendar":
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, cal_handler.execute, data)
        if t == "notion":
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, notion_handler.execute, data)
        if t == "email":
            return {"status": "ok", "note": "copy the draft from the panel"}
        if t == "research":
            return {"status": "ok"}
        return JSONResponse({"error": f"unknown type: {t}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Research ───────────────────────────────────────────────────────────────────

async def _research_draft(action: dict) -> dict:
    prompt = (
        f"Research this topic from a meeting and give a concise briefing.\n\n"
        f"Topic: {action['description']}\n"
        f"Meeting context: {action.get('context', '')}\n\n"
        f"Output 4-6 bullet points. Be specific and factual."
    )
    loop = asyncio.get_event_loop()
    try:
        brief = await loop.run_in_executor(None, lambda: generate_text(prompt, timeout=90))
        return {"brief": brief}
    except LLMError as e:
        return {"error": str(e)[:300]}


# ── Slack ──────────────────────────────────────────────────────────────────────

def _build_slack_message(meeting_name: str, notes_text: str, actions: list) -> str:
    from datetime import datetime
    date_str = datetime.now().strftime("%B %d, %Y at %H:%M")

    summary_lines: list[str] = []
    in_summary = False
    for line in notes_text.splitlines():
        if "## Meeting Summary" in line:
            in_summary = True
            continue
        if in_summary:
            if line.startswith("## "):
                break
            if line.strip():
                summary_lines.append(line)

    pending = [a for a in actions if a.get("status") != "done"]

    msg = f"*📝 {meeting_name or 'Meeting Notes'}*\n_{date_str}_\n\n"
    if summary_lines:
        msg += "*Summary*\n" + "\n".join(summary_lines) + "\n\n"
    if pending:
        msg += f"*Action Items ({len(pending)})*\n"
        for a in pending[:15]:
            owner    = f" — {a['owner']}" if a.get("owner") and a["owner"] not in ("me", "") else ""
            deadline = f" _(by {a['deadline']})_" if a.get("deadline") else ""
            msg += f"• {a['description']}{owner}{deadline}\n"
    msg += "\n_Sent from Meeting Notes_"
    return msg


@app.post("/slack/send")
async def slack_send(request: Request):
    body        = await request.json()
    webhook_url = body.get("webhook_url", "").strip() or SLACK_WEBHOOK_URL
    if not webhook_url:
        return JSONResponse({"error": "No Slack webhook URL — paste one from api.slack.com/apps → Incoming Webhooks"}, status_code=400)
    if not webhook_url.startswith("https://hooks.slack.com/"):
        return JSONResponse({"error": f"URL doesn't look like a Slack webhook — should start with https://hooks.slack.com/services/..."}, status_code=400)

    meeting_name = body.get("meeting_name", "")
    notes_text   = (OUTPUT_DIR / "meeting-notes.md").read_text() if (OUTPUT_DIR / "meeting-notes.md").exists() else ""
    actions      = json.loads(ACTIONS_FILE.read_text()) if ACTIONS_FILE.exists() else []
    message      = _build_slack_message(meeting_name, notes_text, actions)

    def _post():
        data = json.dumps({"text": message}).encode()
        req  = _urllib.Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
        resp = _urllib.urlopen(req, timeout=10)
        return resp.read().decode()

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _post)
        if result.strip() == "ok":
            return {"ok": True}
        return JSONResponse({"error": f"Slack returned unexpected response: {result[:200]}"}, status_code=500)
    except Exception as e:
        err = str(e)
        if "404" in err:
            msg = "Slack webhook not found (404) — the URL may be wrong or the app was removed. Go to api.slack.com/apps → your app → Incoming Webhooks to get a fresh URL."
        elif "403" in err:
            msg = "Slack webhook forbidden (403) — it may have been revoked. Regenerate it at api.slack.com/apps."
        elif "No route" in err or "Connection" in err or "timeout" in err.lower():
            msg = "Network error reaching Slack — check your internet connection."
        else:
            msg = f"Slack error: {err}"
        return JSONResponse({"error": msg}, status_code=500)


# ── Slack: send arbitrary content ─────────────────────────────────────────────

@app.post("/slack/send-content")
async def slack_send_content(request: Request):
    try:
        body        = await request.json()
        webhook_url = (body.get("webhook_url") or "").strip() or SLACK_WEBHOOK_URL
        title       = (body.get("title") or "").strip()
        content     = (body.get("content") or "").strip()

        if not webhook_url:
            return JSONResponse(
                {"error": "No Slack webhook URL — provide webhook_url in the request or set the SLACK_WEBHOOK_URL env var"},
                status_code=400,
            )
        if not webhook_url.startswith("https://hooks.slack.com/"):
            return JSONResponse(
                {"error": "URL doesn't look like a Slack webhook — should start with https://hooks.slack.com/services/..."},
                status_code=400,
            )
        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)

        # Build message: bold title header + body, capped at 3000 chars (Slack block limit)
        header  = f"*{title}*\n" if title else ""
        body_text = content[:3000 - len(header)]
        message = header + body_text

        def _post():
            data = json.dumps({"text": message}).encode()
            req  = _urllib.Request(
                webhook_url, data=data,
                headers={"Content-Type": "application/json"},
            )
            resp = _urllib.urlopen(req, timeout=10)
            return resp.read().decode()

        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _post)
        if result.strip() == "ok":
            return {"ok": True}
        return JSONResponse(
            {"error": f"Slack returned unexpected response: {result[:200]}"},
            status_code=500,
        )
    except Exception as e:
        err = str(e)
        if "404" in err:
            msg = "Slack webhook not found (404) — the URL may be wrong or the app was removed. Go to api.slack.com/apps → your app → Incoming Webhooks to get a fresh URL."
        elif "403" in err:
            msg = "Slack webhook forbidden (403) — it may have been revoked. Regenerate it at api.slack.com/apps."
        elif "No route" in err or "Connection" in err or "timeout" in err.lower():
            msg = "Network error reaching Slack — check your internet connection."
        else:
            msg = f"Slack error: {err}"
        return JSONResponse({"error": msg}, status_code=500)


# ── Notion: markdown → block converter ────────────────────────────────────────

def _md_to_notion_blocks(text: str) -> list:
    """Convert a markdown string to a list of Notion block objects."""
    import re as _re

    def _rich(s: str) -> list:
        """Split inline markdown (bold/italic/code) into rich_text segments."""
        parts: list = []
        for seg in _re.split(r'(\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_|`[^`]+`)', s):
            if not seg:
                continue
            ann = {"bold": False, "italic": False, "code": False}
            content = seg
            if seg.startswith('**') and seg.endswith('**') and len(seg) > 4:
                content, ann["bold"] = seg[2:-2], True
            elif (seg.startswith('*') and seg.endswith('*') and len(seg) > 2
                  and not seg.startswith('**')):
                content, ann["italic"] = seg[1:-1], True
            elif seg.startswith('_') and seg.endswith('_') and len(seg) > 2:
                content, ann["italic"] = seg[1:-1], True
            elif seg.startswith('`') and seg.endswith('`') and len(seg) > 2:
                content, ann["code"] = seg[1:-1], True
            for i in range(0, max(len(content), 1), 2000):
                parts.append({
                    "type": "text",
                    "text": {"content": content[i:i+2000]},
                    "annotations": ann,
                })
        return parts or [{"type": "text", "text": {"content": ""}}]

    blocks: list = []
    for line in text.splitlines():
        s = line.rstrip()
        if s.startswith('### '):
            blocks.append({"object":"block","type":"heading_3","heading_3":{"rich_text":_rich(s[4:])}})
        elif s.startswith('## '):
            blocks.append({"object":"block","type":"heading_2","heading_2":{"rich_text":_rich(s[3:])}})
        elif s.startswith('# '):
            blocks.append({"object":"block","type":"heading_1","heading_1":{"rich_text":_rich(s[2:])}})
        elif re.match(r'^[-*] \[[xX]\] ', s):
            blocks.append({"object":"block","type":"to_do","to_do":{"rich_text":_rich(re.sub(r'^[-*] \[[xX]\] ','',s)),"checked":True}})
        elif re.match(r'^[-*] \[ \] ', s):
            blocks.append({"object":"block","type":"to_do","to_do":{"rich_text":_rich(re.sub(r'^[-*] \[ \] ','',s)),"checked":False}})
        elif re.match(r'^  [-*] ', s):  # indented bullet
            blocks.append({"object":"block","type":"bulleted_list_item","bulleted_list_item":{"rich_text":_rich(s[4:])}})
        elif re.match(r'^[-*] ', s):
            blocks.append({"object":"block","type":"bulleted_list_item","bulleted_list_item":{"rich_text":_rich(s[2:])}})
        elif re.match(r'^\d+\. ', s):
            blocks.append({"object":"block","type":"numbered_list_item","numbered_list_item":{"rich_text":_rich(re.sub(r'^\d+\. ','',s))}})
        elif s in ('---', '***', '___'):
            blocks.append({"object":"block","type":"divider","divider":{}})
        elif s.strip():
            blocks.append({"object":"block","type":"paragraph","paragraph":{"rich_text":_rich(s)}})
    return blocks


# ── Notion: create page from raw content ──────────────────────────────────────

@app.post("/notion/create-page")
async def notion_create_page(request: Request):
    try:
        body    = await request.json()
        title   = (body.get("title") or "").strip()
        content = (body.get("content") or "").strip()

        if not NOTION_TOKEN or not NOTION_DATABASE_ID:
            return JSONResponse(
                {"error": "Notion not configured — set NOTION_TOKEN and NOTION_DATABASE_ID env vars"},
                status_code=400,
            )
        if not title:
            return JSONResponse({"error": "title is required"}, status_code=400)
        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)

        def _create():
            try:
                from notion_client import Client
            except ImportError:
                return {"error": "Run: pip install notion-client"}

            notion     = Client(auth=NOTION_TOKEN)
            properties = {
                "Name": {"title": [{"text": {"content": title}}]},
            }
            # Parse markdown content into proper Notion blocks (headings, bullets, to-dos)
            children = _md_to_notion_blocks(content)
            # Notion API limit: 100 children per request — first 100 inline, rest appended
            first_batch = children[:100]
            rest_batches = [children[i:i+100] for i in range(100, len(children), 100)]

            result = notion.pages.create(
                parent={"database_id": NOTION_DATABASE_ID},
                properties=properties,
                children=first_batch,
            )
            page_id = result.get("id", "")
            for batch in rest_batches:
                notion.blocks.children.append(block_id=page_id, children=batch)
            return {"url": result.get("url", ""), "id": page_id}

        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _create)
        if "error" in result:
            return JSONResponse(result, status_code=500)
        return {"ok": True, "url": result["url"]}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── History (past sessions) ────────────────────────────────────────────────────

@app.get("/history")
async def list_history():
    if not ARCHIVE_DIR.exists():
        return JSONResponse([])
    sessions = []
    for d in sorted(ARCHIVE_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta_file = d / "meta.json"
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
            except Exception:
                pass
        action_count = meta.get("actions", 0)
        if not action_count:
            af = d / "actions.json"
            if af.exists():
                try:
                    action_count = len(json.loads(af.read_text()))
                except Exception:
                    pass
        preview = ""
        notes_f = d / "meeting-notes.md"
        if notes_f.exists():
            for line in notes_f.read_text().splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    preview = stripped[:120]
                    break
        sessions.append({
            "id":        d.name,
            "name":      meta.get("name", ""),
            "timestamp": meta.get("timestamp", d.name[:16].replace("_", " ")),
            "actions":   action_count,
            "preview":   preview,
        })
    return JSONResponse(sessions[:30])


@app.get("/history/{session_id}/{filename}")
async def get_history_file(session_id: str, filename: str):
    allowed = {"transcription.md", "meeting-notes.md", "interview-answers.md",
               "sketch.md", "actions.json"}
    if filename not in allowed:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = ARCHIVE_DIR / Path(session_id).name / filename
    if not path.exists():
        return JSONResponse({"content": ""})
    return JSONResponse({"content": path.read_text()})


@app.patch("/history/{session_id}")
async def rename_session(session_id: str, request: Request):
    session_dir = ARCHIVE_DIR / Path(session_id).name
    if not session_dir.exists():
        return JSONResponse({"error": "session not found"}, status_code=404)
    body = await request.json()
    new_name = body.get("name", "").strip()
    meta_file = session_dir / "meta.json"
    meta: dict = {}
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
        except Exception:
            pass
    meta["name"] = new_name
    meta_file.write_text(json.dumps(meta))
    return {"ok": True}


# ── Config: language ──────────────────────────────────────────────────────────

_LANG_LABELS = {
    "en": "English (US)", "fr": "French", "de": "German", "es": "Spanish",
    "it": "Italian", "pt": "Portuguese", "zh": "Chinese", "ja": "Japanese",
    "ko": "Korean", "nl": "Dutch", "pl": "Polish", "ru": "Russian",
    "ar": "Arabic", "hi": "Hindi", "sv": "Swedish",
}


@app.get("/config/language")
async def get_language():
    lang = _read_config().get("language", "en")
    return {"language": lang, "label": _LANG_LABELS.get(lang, lang)}


@app.post("/config/language")
async def set_language(request: Request):
    body = await request.json()
    lang = body.get("language", "").strip()
    if not lang:
        return JSONResponse({"error": "language is required"}, status_code=400)
    _write_config({"language": lang})
    return {"ok": True, "language": lang, "label": _LANG_LABELS.get(lang, lang)}


# ── Live view ──────────────────────────────────────────────────────────────────

@app.get("/live", response_class=HTMLResponse)
async def live_view():
    notes_file = FILES_MAP.get("notes")
    content = notes_file.read_text() if notes_file and notes_file.exists() else "_No notes yet..._"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="5">
<title>MeetNotes — Live</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
body{{font-family:system-ui;background:#0f1117;color:#e2e8f0;padding:32px;max-width:800px;margin:0 auto;line-height:1.7}}
h1,h2,h3{{color:#a5b4fc}}h2{{margin-top:1.4em}}ul{{padding-left:1.4em}}li{{margin:.3em 0}}
p{{margin:.6em 0}}.muted{{color:#64748b;font-size:.8em;margin-bottom:16px}}
</style></head>
<body>
<p class="muted">Live meeting notes · auto-refreshes every 5s · {datetime.now().strftime('%H:%M')}</p>
<div id="c"></div>
<script>
document.getElementById('c').innerHTML = marked.parse({json.dumps(content)});
</script>
</body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})



@app.get("/health/ai")
async def ai_health():
    return provider_status()


@app.get("/health/app")
async def app_health():
    status = await control_status()
    ai = provider_status()
    return {
        "ok": True,
        "output_dir": str(OUTPUT_DIR),
        "archive_dir": str(ARCHIVE_DIR),
        "recording": status,
        "ai": ai,
    }


# ── Config: active provider ───────────────────────────────────────────────────

@app.get("/config/provider")
async def get_provider_info():
    status = provider_status()
    provider = status["provider"]
    model = status.get("model") or ""
    label = f"OpenAI · {model}" if provider == "openai" else "Claude"
    return {"provider": provider, "model": model, "label": label, "ready": status["ready"], "message": status["message"]}


@app.post("/config/provider")
async def set_provider(request: Request):
    """Update AI provider in .env and reload in-memory config."""
    import importlib
    body = await request.json()
    provider = body.get("provider", "").strip().lower()
    api_key  = body.get("api_key",  "").strip()
    model    = body.get("model",    config.DEFAULT_OPENAI_MODEL).strip() or config.DEFAULT_OPENAI_MODEL

    if provider not in ("claude", "openai"):
        return JSONResponse({"error": "provider must be 'claude' or 'openai'"}, status_code=400)

    updates = {"AI_PROVIDER": provider}
    if api_key:
        updates["OPENAI_API_KEY"] = api_key
    if provider == "openai":
        updates["OPENAI_MODEL"] = model

    try:
        config.write_dotenv(updates)
    except OSError as e:
        return JSONResponse({"error": f"Could not save settings: {e}"}, status_code=500)

    config.refresh_config()
    importlib.reload(__import__("llm"))

    status = provider_status()
    label = f"OpenAI · {status.get('model', model)}" if provider == "openai" else "Claude"
    running = (await control_status()).get("running", False)
    note = "Saved. Restart the current recording for provider changes to affect live agents." if running else "Ready for the next recording."
    return {
        "ok": True,
        "provider": provider,
        "model": status.get("model", model) or "",
        "label": label,
        "ready": status["ready"],
        "message": status["message"],
        "note": note,
    }


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.getenv("MEETINGNOTES_PORT", "8000"))
    url = f"http://localhost:{port}"
    print(f"Meeting Notes → {url}")
    # Open browser only if explicitly requested (pass --open) — otherwise spawning the
    # server from a hot-reload loop opens a new tab on every restart.
    if "--open" in sys.argv:
        import webbrowser
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
