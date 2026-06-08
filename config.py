import os
import shutil
from pathlib import Path

ROOT_DIR = Path(__file__).parent
ENV_FILE = ROOT_DIR / ".env"

DEFAULT_AI_PROVIDER = "openai"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def _parse_dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_dotenv(path: Path = ENV_FILE, *, override: bool = False) -> dict[str, str]:
    values = _parse_dotenv(path)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def write_dotenv(updates: dict[str, str], path: Path = ENV_FILE) -> None:
    existing = path.read_text().splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in existing:
        if "=" not in line or line.strip().startswith("#"):
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out).rstrip() + "\n")
    for key, value in updates.items():
        os.environ[key] = value
    try:
        path.chmod(0o600)
    except OSError:
        pass


def refresh_config() -> None:
    load_dotenv(override=False)
    globals().update({
        "NOTION_TOKEN": os.getenv("NOTION_TOKEN", ""),
        "NOTION_DATABASE_ID": os.getenv("NOTION_DATABASE_ID", ""),
        "GOOGLE_CREDENTIALS_FILE": os.getenv(
            "GOOGLE_CREDENTIALS_FILE",
            str(Path.home() / ".config" / "google" / "meeting-credentials.json"),
        ),
        "GOOGLE_TOKEN_FILE": os.getenv(
            "GOOGLE_TOKEN_FILE",
            str(Path.home() / ".config" / "google" / "meeting-token.json"),
        ),
        "GOOGLE_CALENDAR_TZ": os.getenv("GOOGLE_CALENDAR_TZ", "America/New_York"),
        "AI_PROVIDER": os.getenv("AI_PROVIDER", DEFAULT_AI_PROVIDER).strip().lower(),
        "OPENAI_MODEL": os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip() or DEFAULT_OPENAI_MODEL,
        "SLACK_WEBHOOK_URL": os.getenv("SLACK_WEBHOOK_URL", ""),
    })


def ai_provider() -> str:
    refresh_config()
    return AI_PROVIDER


def openai_model() -> str:
    refresh_config()
    return OPENAI_MODEL


load_dotenv(override=False)

OUTPUT_DIR   = Path.home() / "Desktop" / "meeting-output"
ACTIONS_FILE = OUTPUT_DIR / "actions.json"
AGENT_STATUS_FILE = OUTPUT_DIR / "agent-status.json"
CLAUDE_BIN   = shutil.which("claude") or "claude"
CUSTOM_AGENTS_FILE = Path.home() / ".config" / "meetingnotes" / "custom_agents.json"

refresh_config()
