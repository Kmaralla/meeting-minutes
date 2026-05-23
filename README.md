# meeting-minutes

A live meeting assistant that listens to your mic, transcribes speech in real time, and runs multiple AI agents in parallel to produce structured notes, action items, Q&A answers, and diagrams — all visible in a live web UI.

When the meeting ends, send a summary straight to Slack.

---

## How it works

```
Mic → Whisper (local STT) → 5 Claude agents (parallel)
         ├── Transcriber       → clean timestamped transcript
         ├── Note-taker        → structured meeting notes
         ├── Sketch artist     → Mermaid diagrams
         ├── Q&A agent         → answers every question asked
         └── Action extractor  → email / calendar / Notion / research items
                                         ↓
                              Web UI  (live updates)
                                         ↓
                              Slack DM  (on demand)
```

Agents only run when **you** press **Run Agents** — no background polling, no surprise API calls.

---

## Requirements

- Python 3.10+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated (`claude` on your PATH)
- A microphone

Optional integrations (can be skipped on first run):
- Slack incoming webhook — for end-of-meeting summaries
- Notion API token — to push action items into a database
- Google OAuth credentials — to create calendar events

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/Kmaralla/meeting-minutes
cd meeting-minutes

# 2. Create virtual environment and install deps
python3 -m venv meetingenv
source meetingenv/bin/activate
pip install -r requirements.txt

# 3. Configure (copy and edit)
cp .env.example .env
# Fill in any integrations you want — all optional except Claude CLI

# 4. Start the server
./run.sh --server
# Opens http://localhost:8000 in your browser
```

---

## Usage

1. Open **http://localhost:8000**
2. Type a meeting name in the header
3. Click **Start** — the mic goes live
4. Talk. The assistant listens.
5. Click **Run Agents** whenever you want notes generated (or wait for a natural pause)
6. When done, click **End Meeting** → paste your Slack webhook → **Send to Slack**
7. Click **New Session** to clear everything and start fresh for the next meeting

---

## Configuration

All config is via environment variables. Copy `.env.example` to `.env` and fill in what you need:

```bash
# Required for Slack summaries
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/xxx/yyy/zzz

# Required for Notion action items
NOTION_TOKEN=secret_xxx
NOTION_DATABASE_ID=xxx

# Required for Google Calendar events
GOOGLE_CREDENTIALS_FILE=~/.config/google/meeting-credentials.json
GOOGLE_TOKEN_FILE=~/.config/google/meeting-token.json
GOOGLE_CALENDAR_TZ=America/New_York
```

### Getting a Slack webhook

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → Create App → From Scratch
2. Add feature: **Incoming Webhooks** → Activate
3. Click **Add New Webhook to Workspace** → pick a channel or DM
4. Copy the webhook URL into your `.env`

### Getting Notion credentials

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → New integration
2. Copy the **Internal Integration Token** as `NOTION_TOKEN`
3. Open the database you want to use → Share → Invite your integration
4. Copy the database ID from the URL (the long hex string) as `NOTION_DATABASE_ID`

### Getting Google Calendar credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → Enable **Google Calendar API**
3. Create OAuth 2.0 credentials (Desktop app) → Download JSON
4. Save as `~/.config/google/meeting-credentials.json`
5. First run will open a browser to authenticate

---

## Run modes

```bash
./run.sh              # CLI mode — mic recording, press Enter to run agents
./run.sh --server     # Web UI mode (recommended)
./run.sh --stop       # Stop a running session
./run.sh --status     # Check if a session is running
./run.sh --dispatch-only  # Re-run agents on the last saved transcript
```

---

## Output files

All output is saved to `~/Desktop/meeting-output/` after each agent run:

| File | Contents |
|------|----------|
| `transcription.md` | Timestamped raw transcript |
| `meeting-notes.md` | Summary, key points, decisions, action items |
| `sketch.md` | Mermaid diagrams of any systems or flows discussed |
| `interview-answers.md` | Every question asked with an AI-generated answer |
| `actions.json` | Structured action items for email / calendar / Notion / research |

---

## Whisper model size

By default uses `base` (fast, ~74MB). Swap in `.env` or via CLI flag:

```bash
./run.sh --model small     # better accuracy, ~244MB
./run.sh --model medium    # near human-level, ~769MB
```

| Model | Size | Speed | Accuracy |
|-------|------|-------|----------|
| tiny  | 39MB | fastest | basic |
| base  | 74MB | fast | good |
| small | 244MB | moderate | better |
| medium | 769MB | slow | excellent |

---

## Project structure

```
meeting-minutes/
├── meetingnotes.py   # Core pipeline: mic → Whisper → agents
├── server.py         # FastAPI backend + process control
├── run.sh            # Entry point (start / stop / status)
├── config.py         # Env var loading
├── handlers/
│   ├── calendar.py   # Google Calendar integration
│   ├── email.py      # Email draft generation
│   └── notion.py     # Notion database integration
├── ui/
│   └── index.html    # Single-file web UI
└── requirements.txt
```

---

## License

MIT
