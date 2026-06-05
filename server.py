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
import uuid
import urllib.request as _urllib
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from config import (
    ACTIONS_FILE, CLAUDE_BIN, OUTPUT_DIR,
    SLACK_WEBHOOK_URL, CUSTOM_AGENTS_FILE,
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
    for ca in _load_custom_agents():
        (OUTPUT_DIR / f"custom-{ca['id']}.md").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/control/start")
async def control_start():
    global _proc, _proc_start_time
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
    for ca in _load_custom_agents():
        (OUTPUT_DIR / f"custom-{ca['id']}.md").unlink(missing_ok=True)
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
        pid = _proc.pid
        _proc.send_signal(_signal.SIGINT)
        asyncio.create_task(_force_kill_after(pid, delay=90))
        return {"ok": True}
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, _signal.SIGINT)
            asyncio.create_task(_force_kill_after(pid, delay=90))
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


# ── Multiplexed SSE — ONE connection for all panels ───────────────────────────
# Replaces the 5-7 individual SSE streams that used to hit the browser's
# HTTP/1.1 per-origin connection limit (6), causing fetch() requests to queue.

async def _stream_all_events() -> AsyncGenerator[str, None]:
    last: dict[str, str | None] = {name: None for name in FILES_MAP}
    last["actions"] = None
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

        # Actions file
        try:
            if ACTIONS_FILE.exists():
                raw = ACTIONS_FILE.read_text()
                if raw != last["actions"]:
                    last["actions"] = raw
                    data = json.loads(raw) if raw.strip() else []
                    yield f"data: {json.dumps({'type': 'actions', 'data': data})}\n\n"
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
    if ACTIONS_FILE.exists():
        try:
            return JSONResponse(json.loads(ACTIONS_FILE.read_text()))
        except Exception:
            pass
    return JSONResponse([])




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


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    import uvicorn
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Meeting Notes → http://localhost:8000")
    webbrowser.open("http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
