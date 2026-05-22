#!/usr/bin/env python3
"""
server.py — Meeting Notes UI backend

Start: python server.py  (or ./run.sh --server)
Opens: http://localhost:8000
"""

import asyncio
import json
import os
import signal as _signal
import subprocess
import sys
import time
import urllib.request as _urllib
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from config import (
    ACTIONS_FILE, CLAUDE_BIN, OUTPUT_DIR,
    SLACK_WEBHOOK_URL,
)
from handlers import calendar as cal_handler
from handlers import email as email_handler
from handlers import notion as notion_handler

app = FastAPI(title="Meeting Notes")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

UI_DIR          = Path(__file__).parent / "ui"
UI_FILE         = UI_DIR / "index.html"
PID_FILE        = Path("/tmp/meetingnotes.pid")
DISPATCH_FILE   = Path("/tmp/meetingnotes.dispatch")

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


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return UI_FILE.read_text()


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
    return {"running": running, "elapsed": elapsed}


@app.post("/control/new-session")
async def control_new_session():
    global _proc, _proc_start_time
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
    return {"ok": True}


@app.post("/control/start")
async def control_start():
    global _proc, _proc_start_time
    if _proc and _proc.poll() is None:
        return JSONResponse({"ok": False, "error": "already running"}, status_code=409)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Clear stale output files so UI shows fresh content
    for path in FILES_MAP.values():
        if path.exists():
            path.unlink()
    if ACTIONS_FILE.exists():
        ACTIONS_FILE.unlink()
    log_file = open(OUTPUT_DIR / "session.log", "w")
    _proc = subprocess.Popen(
        [PYTHON_BIN, str(MEETINGNOTES_PY)],
        stdin=subprocess.PIPE,
        stdout=log_file,
        stderr=log_file,
        text=True,
        cwd=str(Path(__file__).parent),
    )
    _proc_start_time = time.time()
    return {"ok": True, "pid": _proc.pid}


@app.post("/control/stop")
async def control_stop():
    global _proc
    if _proc and _proc.poll() is None:
        _proc.send_signal(_signal.SIGINT)
        return {"ok": True}
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, _signal.SIGINT)
            return {"ok": True}
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


# ── File endpoints ─────────────────────────────────────────────────────────────

async def _stream_file(name: str) -> AsyncGenerator[str, None]:
    path = FILES_MAP.get(name)
    if not path:
        return
    last_content = None
    while True:
        try:
            if path.exists():
                content = path.read_text()
                if content != last_content:
                    last_content = content
                    yield f"data: {json.dumps({'content': content})}\n\n"
        except Exception:
            pass
        await asyncio.sleep(2)


@app.get("/files/stream/{name}")
async def files_stream(name: str):
    if name not in FILES_MAP:
        return JSONResponse({"error": "unknown file"}, status_code=404)
    return StreamingResponse(
        _stream_file(name),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/files/{name}")
async def files_get(name: str):
    path = FILES_MAP.get(name)
    if not path or not path.exists():
        return JSONResponse({"content": ""})
    return JSONResponse({"content": path.read_text()})


# ── Actions REST ───────────────────────────────────────────────────────────────

@app.get("/actions")
async def get_actions():
    if ACTIONS_FILE.exists():
        try:
            return JSONResponse(json.loads(ACTIONS_FILE.read_text()))
        except Exception:
            pass
    return JSONResponse([])


async def _stream_actions() -> AsyncGenerator[str, None]:
    last = ""
    while True:
        if ACTIONS_FILE.exists():
            try:
                content = ACTIONS_FILE.read_text()
                if content != last:
                    last = content
                    yield f"data: {content}\n\n"
            except Exception:
                pass
        await asyncio.sleep(2)


@app.get("/actions/stream")
async def actions_stream():
    return StreamingResponse(
        _stream_actions(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


# ── Act: execute ───────────────────────────────────────────────────────────────

@app.post("/act/execute")
async def execute_action(request: Request):
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


# ── Research ───────────────────────────────────────────────────────────────────

async def _research_draft(action: dict) -> dict:
    prompt = (
        f"Research this topic from a meeting and give a concise briefing.\n\n"
        f"Topic: {action['description']}\n"
        f"Meeting context: {action.get('context', '')}\n\n"
        f"Output 4-6 bullet points. Be specific and factual."
    )
    loop = asyncio.get_event_loop()
    proc = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--tools", ""],
            capture_output=True, text=True, timeout=90,
        ),
    )
    if proc.returncode == 0:
        return {"brief": proc.stdout.strip()}
    return {"error": proc.stderr.strip()[:300]}


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
        return JSONResponse({"error": "No Slack webhook URL provided"}, status_code=400)

    meeting_name = body.get("meeting_name", "")
    notes_text   = (OUTPUT_DIR / "meeting-notes.md").read_text() if (OUTPUT_DIR / "meeting-notes.md").exists() else ""
    actions      = json.loads(ACTIONS_FILE.read_text()) if ACTIONS_FILE.exists() else []
    message      = _build_slack_message(meeting_name, notes_text, actions)

    def _post():
        data = json.dumps({"text": message}).encode()
        req  = _urllib.Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
        _urllib.urlopen(req, timeout=10)

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _post)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    import uvicorn
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Meeting Notes → http://localhost:8000")
    webbrowser.open("http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
