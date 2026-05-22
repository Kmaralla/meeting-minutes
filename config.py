import os
import shutil
from pathlib import Path

NOTION_TOKEN          = os.getenv("NOTION_TOKEN", "")
NOTION_DATABASE_ID    = os.getenv("NOTION_DATABASE_ID", "")

GOOGLE_CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS_FILE",
    str(Path.home() / ".config" / "google" / "meeting-credentials.json"),
)
GOOGLE_TOKEN_FILE = os.getenv(
    "GOOGLE_TOKEN_FILE",
    str(Path.home() / ".config" / "google" / "meeting-token.json"),
)
GOOGLE_CALENDAR_TZ = os.getenv("GOOGLE_CALENDAR_TZ", "America/New_York")

OUTPUT_DIR   = Path.home() / "Desktop" / "meeting-output"
ACTIONS_FILE = OUTPUT_DIR / "actions.json"
CLAUDE_BIN        = shutil.which("claude") or "claude"
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
