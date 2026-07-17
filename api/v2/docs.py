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

# Primary public documents, keyed by their stable short name. The names mirror
# the layout under docs/: <area>/<file>. Backward-compatibility aliases below
# keep older bookmarks and links working.
ALLOWED_DOCS = {
    "readme": PROJECT_ROOT / "README.md",
    "changelog": PROJECT_ROOT / "CHANGELOG.md",
    "agent-readme": PROJECT_ROOT / "AGENT_README.md",
    # concepts
    "concepts": DOCS_DIR / "concepts.md",
    "getting-started": DOCS_DIR / "getting-started.md",
    # server
    "server-setup": DOCS_DIR / "server" / "setup.md",
    "server-admin": DOCS_DIR / "server" / "admin.md",
    "server-dashboard": DOCS_DIR / "server" / "dashboard.md",
    # node
    "node-setup": DOCS_DIR / "node" / "setup.md",
    "node-cli-reference": DOCS_DIR / "node" / "cli-reference.md",
    "node-capabilities": DOCS_DIR / "node" / "capabilities.md",
    "node-token-lifecycle": DOCS_DIR / "node" / "token-lifecycle.md",
    # reference
    "reference-api": DOCS_DIR / "reference" / "api.md",
    "reference-design-board": DOCS_DIR / "reference" / "design-board.md",
}

# Legacy short names that now resolve to the same files as their new primary
# counterparts. They are kept so existing bookmarks, the dashboard redirect,
# and the login-page link do not break.
_LEGACY_ALIASES = {
    "setup": "server-setup",
    "admin-setup": "server-admin",
    "dashboard": "server-dashboard",
    "node-readme": "node-setup",
    "nodes-design": "concepts",
    "token-concept": "concepts",
    "token-lifecycle": "node-token-lifecycle",
    "capabilities": "node-capabilities",
    "design-board": "reference-design-board",
    "proxmox-worker-setup": "node-setup",
}


def _resolve(name: str):
    """Return the path for a doc name, resolving legacy aliases."""
    if name in ALLOWED_DOCS:
        return ALLOWED_DOCS[name]
    alias = _LEGACY_ALIASES.get(name)
    if alias is not None:
        return ALLOWED_DOCS.get(alias)
    return None


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
    names return 404. Legacy names are resolved to their current files.
    """
    path = _resolve(doc_name)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    content = _render_markdown(path)
    return HTMLResponse(content=content)