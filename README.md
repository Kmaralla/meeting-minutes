<div align="center">

# meeting-minutes

**A local AI meeting assistant that listens, transcribes, and thinks in real time.**

Your mic → Whisper (local STT) → five Claude agents running in parallel → live notes, action items, Q&A answers, diagrams, and custom agents — all in a browser tab. Nothing leaves your machine except the Claude API calls.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Whisper](https://img.shields.io/badge/Whisper-faster--whisper-orange)](https://github.com/SYSTRAN/faster-whisper)
[![Claude](https://img.shields.io/badge/Claude-Code%20CLI-blueviolet?logo=anthropic&logoColor=white)](https://claude.ai/code)
[![License](https://img.shields.io/badge/License-MIT-green)](#license)

</div>

---

## What it does

Start recording. Talk. The transcript appears word-by-word as you speak. Every few minutes — automatically, without you clicking anything — five AI agents read the transcript and update their panels live:

| Agent | What you see |
|---|---|
| **Note-taker** | Running summary, key points, decisions, open questions |
| **Action extractor** | Tasks as cards — tagged email / calendar / Notion / research, with one-click execute |
| **Q&A agent** | Every question asked in the meeting, answered in real time |
| **Sketch artist** | Mermaid diagrams auto-generated for any system, workflow, or process discussed |
| **Transcriber** | Clean, timestamped, filler-word-free transcript |

You can also add **custom agents** from the UI — write any prompt and get a dedicated live-updating tab. Sales coaching, risk analysis, technical review — whatever your meeting needs.

When you're done: one click sends a summary + action items to Slack.

---

## How it works

```
Microphone
    │
    ▼
faster-whisper (local, offline)
    │  transcribes each speech segment (~2s chunks)
    ▼
transcription.md  ◄─── written immediately, UI updates live
    │
    │  every ~5 new chunks (auto) or "Generate Notes" (manual)
    ▼
┌─────────────────────────────────────────────────┐
│  5 built-in agents  +  N custom agents          │
│  all running in parallel via Claude Code CLI    │
│                                                 │
│  note-taker · action-extractor · Q&A agent      │
│  sketch-artist · transcriber · [your agents]    │
└─────────────────────────────────────────────────┘
    │
    ▼
Output files (~/Desktop/meeting-output/)
    │
    ▼
FastAPI SSE → Browser (live updates every 0.5s)
    │
    ▼
Slack webhook (on "End Meeting")
```

Key design decisions:
- **All audio processing is local** — faster-whisper runs on CPU, no audio sent anywhere
- **Agents auto-dispatch** — every ~5 speech chunks (~2-3 min of talking), no button needed
- **Custom agents persist** — defined once in `~/.config/meetingnotes/`, run every dispatch
- **No build step** — vanilla JS frontend, single HTML file

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.10+** | `python3 --version` to check |
| **Claude Code CLI** | Install at [claude.ai/code](https://claude.ai/code) — run `claude --version` to verify |
| **A microphone** | Built-in or external, any OS mic works |
| **macOS or Linux** | Windows works but `sounddevice` setup may vary |

Claude Code must be **authenticated** before starting:
```bash
claude  # opens browser auth if not logged in
```

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/Kmaralla/meeting-minutes
cd meeting-minutes

# 2. Create virtual environment
python3 -m venv meetingenv
source meetingenv/bin/activate      # Windows: meetingenv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Configure integrations — Slack, Notion, Google Calendar
cp .env.example .env
# Edit .env with your keys — all optional, can be skipped

# 5. Start
./run.sh --server
# Opens http://localhost:8000
```

First run downloads the Whisper `base` model (~74 MB) — takes about 30 seconds.

---

## Usage

1. Open **http://localhost:8000**
2. Type a meeting name (optional)
3. Click **Start Recording** — mic goes live, transcript appears as you speak
4. Talk normally — agents auto-run every few minutes and update all panels
5. Click **Generate Notes** any time to force an immediate agent run
6. Click **End Meeting** when done — agents do a final pass, then send to Slack
7. Click **New Session** to clear everything for the next meeting

### Adding a custom agent

Click **+ Agent** in the tab bar → give it a name and a prompt → **Add Agent**.

Three quick-start templates are built in:
- **Sales Coach** — objection handling, buying signals, next questions to ask
- **Tech Reviewer** — architectural risks, unclear requirements, trade-offs
- **Risk Analyst** — live risk register with high/medium/low ratings

Your agent runs automatically on every dispatch alongside the built-in five. Its definition is saved to `~/.config/meetingnotes/` and persists across sessions.

---

## Configuration

Copy `.env.example` to `.env`. All variables are optional:

```bash
# Slack — send meeting summary + actions at the end
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...

# Notion — push action items into a database
NOTION_TOKEN=secret_...
NOTION_DATABASE_ID=...

# Google Calendar — create calendar events from action items
GOOGLE_CREDENTIALS_FILE=~/.config/google/meeting-credentials.json
GOOGLE_TOKEN_FILE=~/.config/google/meeting-token.json
GOOGLE_CALENDAR_TZ=America/New_York

# Whisper model (default: base)
# WHISPER_MODEL=base
```

### Slack webhook

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create App** → From Scratch
2. **Incoming Webhooks** → toggle on → **Add New Webhook to Workspace**
3. Choose a channel or DM → copy the URL into `.env`

### Notion

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New integration** → copy the token
2. Open your target database → **Share** → invite your integration
3. Copy the database ID from the URL (the long hex after the workspace slug)

### Google Calendar

1. [console.cloud.google.com](https://console.cloud.google.com) → new project → enable **Google Calendar API**
2. **Credentials** → Create → **OAuth 2.0 Client ID** → Desktop app → download JSON
3. Save the file to `~/.config/google/meeting-credentials.json`
4. First use opens a browser tab to complete OAuth — token is saved automatically

---

## Whisper model sizes

Default is `base` — fast and accurate enough for most meetings. Pass `--model` to `run.sh` to change:

```bash
./run.sh --server --model small    # better with accents / technical terms
./run.sh --server --model medium   # near human-level accuracy, slower
```

| Model | Size | Speed | Best for |
|---|---|---|---|
| `tiny` | 39 MB | Fastest | Quick tests |
| `base` | 74 MB | Fast | **Default** — works well for most meetings |
| `small` | 244 MB | Medium | Accents, technical vocabulary |
| `medium` | 769 MB | Slow on CPU | Maximum accuracy |

All models run fully offline. No audio leaves your machine.

---

## Server commands

```bash
# Start (recommended — opens browser UI)
./run.sh --server

# Stop (from another terminal)
pkill -f "python.*server.py"

# Restart
pkill -f "python.*server.py" && sleep 1 && ./run.sh --server

# If port 8000 is stuck in use
lsof -ti :8000 | xargs kill -9 && sleep 1 && ./run.sh --server

# Re-run agents on a saved transcript without the mic
./run.sh --dispatch-only
```

---

## Output files

All written to `~/Desktop/meeting-output/` during a session:

| File | Contents |
|---|---|
| `transcription.md` | Live transcript — updates as you speak |
| `meeting-notes.md` | Summary, key points, decisions, action items, open questions |
| `sketch.md` | Mermaid diagrams for systems and workflows discussed |
| `interview-answers.md` | Every question detected, with AI-generated answers |
| `actions.json` | Structured action items (type, owner, deadline, context, status) |
| `custom-{id}.md` | Output from each custom agent |

Custom agent definitions are stored separately at `~/.config/meetingnotes/custom_agents.json` and persist across sessions.

---

## Project structure

```
meeting-minutes/
├── meetingnotes.py       # Core pipeline: mic → Whisper → agents → files
├── server.py             # FastAPI backend: process control, SSE, REST, custom agents
├── config.py             # Env var loading + shared paths
├── run.sh                # Entry point for all run modes
├── handlers/
│   ├── calendar.py       # Google Calendar integration
│   ├── email.py          # Email draft generation
│   └── notion.py         # Notion database integration
├── ui/
│   ├── index.html        # Single-file web UI — no framework, no build step
│   ├── marked.min.js     # Markdown renderer (self-hosted)
│   └── mermaid.min.js    # Diagram renderer (self-hosted)
├── .env.example          # Configuration template
└── requirements.txt
```

---

## Troubleshooting

**Nothing transcribed / mic not picking up**

The default silence threshold may be too high for your mic. Check `session.log` in `~/Desktop/meeting-output/` — if you see `chunks 0` after speaking, lower the threshold in `meetingnotes.py`:
```python
SILENCE_THRESHOLD = 0.003  # try lower values if speech isn't detected
```

**`claude` not found**

Make sure Claude Code CLI is installed and on your PATH:
```bash
which claude          # should print a path
claude --version      # should print a version
```
If not found, install from [claude.ai/code](https://claude.ai/code) and re-open your terminal.

**Port 8000 already in use**

```bash
lsof -ti :8000 | xargs kill -9
./run.sh --server
```

**Agents run but output panels stay empty**

Hard-refresh the browser (`Cmd+Shift+R` / `Ctrl+Shift+R`). If that doesn't help, check `session.log` for agent errors — usually a missing `claude` binary or API auth issue.

**`faster-whisper` install fails**

On some systems you need to install `ctranslate2` separately:
```bash
pip install ctranslate2 faster-whisper
```

---

## Tech stack

| Layer | Technology | Why |
|---|---|---|
| Speech-to-text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Runs fully local, CPU-friendly, no API key |
| AI agents | [Claude Code CLI](https://claude.ai/code) | Invoked as subprocesses, parallel execution |
| Backend | [FastAPI](https://fastapi.tiangolo.com/) + SSE | Lightweight, async, real-time streaming |
| Frontend | Vanilla JS | Zero dependencies, no build step, single file |
| Integrations | Slack, Notion, Google Calendar | Action execution from within the UI |

---

## Contributing

PRs welcome. Things that would be great to add:

- [ ] Speaker diarization (who said what)
- [ ] Export to PDF / Notion page
- [ ] Meeting templates (standup, interview, design review)
- [ ] Multi-language support
- [ ] Windows tested setup guide

Open an issue first for anything large so we can align before you build.

---

## License

MIT — use it, fork it, build on it.
