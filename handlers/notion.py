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
        from notion_client.errors import APIResponseError
    except ImportError:
        return {"error": "Run: pip install notion-client"}

    notion = Client(auth=NOTION_TOKEN)
    db_id  = page.get("database_id") or NOTION_DATABASE_ID

    # Build context quote block
    children = []
    if page.get("context"):
        children.append({
            "object": "block",
            "type":   "quote",
            "quote":  {"rich_text": [{"type": "text", "text": {"content": page["context"][:2000]}}]},
        })

    def _create(properties: dict) -> dict:
        result = notion.pages.create(
            parent={"database_id": db_id},
            properties=properties,
            children=children,
        )
        return {"url": result.get("url", ""), "id": result.get("id", "")}

    # Try with all optional properties first; fall back to title-only if the
    # database doesn't have Due Date / Owner columns
    properties: dict = {
        "Name": {"title": [{"text": {"content": page["title"][:2000]}}]},
    }
    if page.get("deadline"):
        properties["Due Date"] = {"date": {"start": page["deadline"]}}
    if page.get("owner"):
        properties["Owner"] = {"rich_text": [{"text": {"content": page["owner"][:200]}}]}

    try:
        return _create(properties)
    except APIResponseError as e:
        # Property validation failed — retry with title only
        if "validation" in str(e).lower() or "property" in str(e).lower():
            try:
                return _create({"Name": properties["Name"]})
            except APIResponseError as e2:
                return {"error": str(e2)[:300]}
        return {"error": str(e)[:300]}
    except Exception as e:
        return {"error": str(e)[:300]}
