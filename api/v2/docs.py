"""Public documentation router.

Serves Markdown documents from the repository as HTML under
/relay/v2/docs/{name}. Documents are discovered mechanically from the
docs/ tree (slug = path relative to DOCS_DIR, "/" joined with "-"), so
adding a page never requires a server-code change. Path traversal is
prevented by construction: slugs are flat, dotfiles are excluded, only
*.md files are discovered. Established short slugs (dashboard links,
node-cli docs examples) win over mechanical names via _SLUG_OVERRIDES;
legacy aliases keep old bookmarks resolving.
"""

import re
from pathlib import Path

import markdown
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

PROJECT_ROOT = Path(__file__).parent.parent.parent
DOCS_DIR = PROJECT_ROOT / "docs"

# Project-root extras served alongside the docs/ tree. Outside discovery
# because they are not files under docs/. Missing files simply report
# available: false in the index (same behaviour as the old whitelist).
_ALLOW_EXTRAS = {
    "readme": PROJECT_ROOT / "README.md",
    "changelog": PROJECT_ROOT / "CHANGELOG.md",
    "agent-readme": PROJECT_ROOT / "AGENT_README.md",
}

# Established slugs whose mechanical name deviates (dashboard static
# pages, login-page link, node-cli docs examples). These win over the
# mechanical rule. Updated to the T-215-B tree (node/ha-node.md moved
# to node/integrations/home-assistant.md; node-config became the exact
# mechanical slug of node/config.md — no override needed).
_SLUG_OVERRIDES = {
    "node-ha": DOCS_DIR / "node" / "integrations" / "home-assistant.md",
}

# Legacy short names that resolve to primary slugs. Kept so existing
# bookmarks, the dashboard redirect and the login-page link do not break.
# Row 2 (added T-215-B): pre-rewrite slugs whose target file moved —
# they heal onto the new pages. Renames after T-215 must NOT add
# further aliases (the alias era ended; slugs are stable now).
_LEGACY_ALIASES = {
    "setup": "server-setup",
    "admin-setup": "server-admin",
    "dashboard": "server-dashboard",
    "node-readme": "node-setup",
    "nodes-design": "concepts-overview",
    "token-concept": "concepts-tokens",
    "token-lifecycle": "node-tokens",
    "capabilities": "node-capabilities",
    "design-board": "reference-design-board",
    "proxmox-worker-setup": "node-setup",
    "api-reference": "reference-api",
    # T-215-B bridge: pre-rewrite slugs → new pages
    "concepts": "concepts-overview",
    "node-token-lifecycle": "node-tokens",
    "node-cli-reference": "node-cli",
    "node-node-daemon": "node-operations",
    "node-node-config": "node-config",
    "node-hermes-integration": "node-integrations-hermes",
    "node-capability-concept": "concepts-capabilities",
    "node-concept": "concepts-nodes",
    "node-federation": "federation-concept",
    "storage-qnap-storage-node": "storage-qnap",
    "reference-database-backends": "reference-database-backends",  # unchanged, kept explicit
}


def _discover_docs() -> dict[str, Path]:
    """Slug -> file map over DOCS_DIR/**.md.

    Mechanical rule: slug = path relative to DOCS_DIR without the .md
    suffix, directory parts joined with "-" (docs/server/setup.md ->
    "server-setup"). Dotfiles/dot-directories are excluded. Intentionally
    uncached: the tree is small and a changed docs/ checkout (submodule
    bump) is picked up on the next request without a restart.
    """
    mapping: dict[str, Path] = {}
    for p in sorted(DOCS_DIR.rglob("*.md")):
        rel = p.relative_to(DOCS_DIR)
        if any(part.startswith(".") for part in rel.parts):
            continue
        mapping["-".join(rel.with_suffix("").parts)] = p
    return mapping


def _primary_map() -> dict[str, Path]:
    """All resolvable primary slugs: root extras + discovery + overrides."""
    mapping = _discover_docs()
    for slug, path in _SLUG_OVERRIDES.items():
        if path.exists():
            mapping[slug] = path
    return {**_ALLOW_EXTRAS, **mapping}


def _resolve(name: str):
    """Return the file path for a doc slug, resolving legacy aliases."""
    slug = name.strip("/")
    if not slug or "/" in slug or ".." in slug:
        return None
    primary = _primary_map()
    if slug in primary:
        return primary[slug]
    alias = _LEGACY_ALIASES.get(slug)
    if alias is not None:
        return primary.get(alias)
    return None


def _rewrite_links(html: str, source_path: Path) -> str:
    """Rewrite relative .md links in rendered HTML to /relay/v2/docs/{name} URLs.

    Links that point to a resolvable document are rewritten so they work
    inside the browser. External links and links to unknown files are
    left untouched. The reverse map prefers established override slugs
    (insertion order: mechanical discovery first, overrides last).
    """
    source_dir = source_path.resolve().parent
    path_to_slug = {str(p.resolve()): slug for slug, p in _primary_map().items()}

    def _replace(match: re.Match) -> str:
        href = match.group(1)
        # Only rewrite relative .md links
        if not href.endswith(".md") or href.startswith(("http://", "https://", "/", "#")):
            return match.group(0)
        # Resolve relative to the source document's directory
        target = (source_dir / href).resolve()
        doc_name = path_to_slug.get(str(target))
        if doc_name is None:
            return match.group(0)  # leave unknown links as-is
        return f'href="/relay/v2/docs/{doc_name}"'

    return re.sub(r'href="([^"]+)"', _replace, html)


def _render_markdown(path: Path) -> str:
    md = path.read_text(encoding="utf-8")
    html = markdown.markdown(md, extensions=["fenced_code", "tables"])
    html = _rewrite_links(html, path)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{path.stem} — IOWAP Docs</title>
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
</html>""".strip()


@router.get("", include_in_schema=False)
async def docs_index():
    """List all public documents (primary slugs; legacy aliases excluded)."""
    items = []
    for name, path in _primary_map().items():
        items.append({
            "name": name,
            "title": path.stem,
            "url": f"/relay/v2/docs/{name}",
            "available": path.exists(),
        })
    return JSONResponse({"docs": items})


@router.get("/{doc_name}", include_in_schema=False)
async def docs_page(doc_name: str):
    """Render a discovered Markdown document as HTML.

    Unknown names return 404. Legacy names resolve to their primary
    slugs' files.
    """
    path = _resolve(doc_name)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    content = _render_markdown(path)
    return HTMLResponse(content=content)