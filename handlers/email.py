import subprocess
from config import CLAUDE_BIN


def draft(action: dict) -> dict:
    prompt = f"""Draft a concise professional follow-up email for this meeting action item.

Action: {action['description']}
Owner: {action.get('owner', 'me')}
Meeting context: {action.get('context', '')}

Output the email in two parts separated by exactly this delimiter: ---BODY---
First line: the subject line (no "Subject:" prefix)
Then the delimiter
Then the email body only — greeting, content, sign-off.
No meta-commentary."""

    proc = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--tools", ""],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()[:300]}

    parts = proc.stdout.strip().split("---BODY---", 1)
    subject = parts[0].strip() if len(parts) > 1 else "Meeting follow-up"
    body    = parts[1].strip() if len(parts) > 1 else proc.stdout.strip()
    return {"subject": subject, "body": body}
