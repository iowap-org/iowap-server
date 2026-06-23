"""Public documentation router.

Serves selected Markdown documents from the repository as HTML under
/relay/v2/docs/{name}. The whitelist prevents path traversal.
"""

from pathlib import Path

import markdown
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent
DOCS_DIR = PROJECT_ROOT / "docs"

ALLOWED_DOCS = {
    "readme": PROJECT_ROOT / "README.md",
    "changelog": PROJECT_ROOT / "CHANGELOG.md",
    "agent-readme": PROJECT_ROOT / "AGENT_README.md",
    "node-readme": DOCS_DIR / "node-readme.md",
    "token-concept": DOCS_DIR / "token-concept.md",
    "dashboard": DOCS_DIR / "dashboard.md",
    "setup": DOCS_DIR / "setup.md",
    "nodes-design": DOCS_DIR / "nodes-design.md",
    "adr-001-node-id-schema": DOCS_DIR / "adr" / "adr-001-node-id-schema.md",
    "adr-002-bootstrap-and-recovery": DOCS_DIR / "adr" / "adr-002-bootstrap-and-recovery.md",
}


def _render_markdown(path: Path) -> str:
    md = path.read_text(encoding="utf-8")
    html = markdown.markdown(md, extensions=["fenced_code", "tables"])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{path.stem} — AI Relay Docs</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 0 auto; max-width: 800px; padding: 2rem 1rem; background: #0b0d11; color: #e0e2e8; line-height: 1.6; }}
    a {{ color: #7aa2ff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    h1, h2, h3 {{ color: #fff; border-bottom: 1px solid #2a2f3a; padding-bottom: .25rem; }}
    code {{ background: #1a1d25; padding: .15rem .35rem; border-radius: .25rem; }}
    pre {{ background: #1a1d25; padding: 1rem; border-radius: .5rem; overflow-x: auto; }}
    pre code {{ background: transparent; padding: 0; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #2a2f3a; padding: .5rem; text-align: left; }}
    th {{ background: #1a1d25; }}
  </style>
</head>
<body>
  {html}
</body>
</html>
""".strip()


@router.get("", include_in_schema=False)
async def docs_index():
    """List public documents."""
    items = []
    for name, path in ALLOWED_DOCS.items():
        items.append({
            "name": name,
            "title": path.stem,
            "url": f"/relay/v2/docs/{name}",
            "available": path.exists(),
        })
    return JSONResponse({"docs": items})


@router.get("/{doc_name}", include_in_schema=False)
async def docs_page(doc_name: str):
    """Render a public Markdown document as HTML.

    The whitelist maps short names to files inside the repository. Unknown
    names return 404.
    """
    path = ALLOWED_DOCS.get(doc_name)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    content = _render_markdown(path)
    return HTMLResponse(content=content)
