from llm import LLMError, generate_text


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

    try:
        output = generate_text(prompt, timeout=60)
    except LLMError as e:
        return {"error": str(e)[:300]}

    parts = output.strip().split("---BODY---", 1)
    subject = parts[0].strip() if len(parts) > 1 else "Meeting follow-up"
    body    = parts[1].strip() if len(parts) > 1 else output.strip()
    return {"subject": subject, "body": body}
