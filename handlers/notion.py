from config import NOTION_TOKEN, NOTION_DATABASE_ID


def draft(action: dict) -> dict:
    """Return page preview before creating."""
    return {
        "title":       action["description"],
        "owner":       action.get("owner", ""),
        "deadline":    action.get("deadline", ""),
        "context":     action.get("context", ""),
        "database_id": NOTION_DATABASE_ID,
    }


def execute(page: dict) -> dict:
    """Create a task page in the configured Notion database."""
    if not NOTION_TOKEN:
        return {"error": "NOTION_TOKEN env var not set"}
    if not NOTION_DATABASE_ID:
        return {"error": "NOTION_DATABASE_ID env var not set"}

    try:
        from notion_client import Client
    except ImportError:
        return {"error": "Run: pip install notion-client"}

    notion = Client(auth=NOTION_TOKEN)

    properties: dict = {
        "Name": {"title": [{"text": {"content": page["title"]}}]},
    }
    if page.get("deadline"):
        try:
            properties["Due Date"] = {"date": {"start": page["deadline"]}}
        except Exception:
            pass
    if page.get("owner"):
        properties["Owner"] = {"rich_text": [{"text": {"content": page["owner"]}}]}

    children = []
    if page.get("context"):
        children.append({
            "object": "block",
            "type":   "quote",
            "quote":  {
                "rich_text": [{"type": "text", "text": {"content": page["context"]}}]
            },
        })

    result = notion.pages.create(
        parent={"database_id": page.get("database_id") or NOTION_DATABASE_ID},
        properties=properties,
        children=children,
    )
    return {"url": result.get("url", ""), "id": result.get("id", "")}
