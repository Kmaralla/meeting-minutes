from pathlib import Path
from datetime import datetime, timedelta
from config import GOOGLE_CREDENTIALS_FILE, GOOGLE_TOKEN_FILE, GOOGLE_CALENDAR_TZ


def draft(action: dict) -> dict:
    """Return structured event preview — no API call yet."""
    deadline = action.get("deadline", "")
    try:
        start_dt = datetime.fromisoformat(deadline) if deadline else _tomorrow_at(10)
    except ValueError:
        start_dt = _tomorrow_at(10)

    return {
        "summary":          action["description"],
        "start":            start_dt.strftime("%Y-%m-%dT%H:%M"),
        "duration_minutes": 60,
        "description":      action.get("context", ""),
        "attendees":        [],
        "timezone":         GOOGLE_CALENDAR_TZ,
    }


def execute(event: dict) -> dict:
    """Create the event in Google Calendar."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from google.auth.transport.requests import Request
    except ImportError:
        return {"error": "Run: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"}

    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    creds = None

    if Path(GOOGLE_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(GOOGLE_CREDENTIALS_FILE).exists():
                return {"error": f"Credentials not found at {GOOGLE_CREDENTIALS_FILE}. Set GOOGLE_CREDENTIALS_FILE."}
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(GOOGLE_TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(GOOGLE_TOKEN_FILE).write_text(creds.to_json())

    service = build("calendar", "v3", credentials=creds)

    try:
        start_dt = datetime.fromisoformat(event.get("start", ""))
    except (ValueError, TypeError):
        start_dt = _tomorrow_at(10)

    end_dt = start_dt + timedelta(minutes=int(event.get("duration_minutes", 60)))
    tz = event.get("timezone", GOOGLE_CALENDAR_TZ)

    body = {
        "summary":     event["summary"],
        "description": event.get("description", ""),
        "start":       {"dateTime": start_dt.isoformat(), "timeZone": tz},
        "end":         {"dateTime": end_dt.isoformat(),   "timeZone": tz},
    }
    attendees = event.get("attendees", [])
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees if e]

    result = service.events().insert(calendarId="primary", body=body).execute()
    return {"link": result.get("htmlLink", ""), "id": result.get("id", "")}


def _tomorrow_at(hour: int) -> datetime:
    return (datetime.now() + timedelta(days=1)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
