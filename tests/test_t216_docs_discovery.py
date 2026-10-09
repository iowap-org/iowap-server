"""T-216: Docs portal — directory discovery instead of the static whitelist.

The old ALLOWED_DOCS whitelist served only 17 of 20+ markdown files under
docs/ (11 pages unreachable, and every new page required a server-code
edit). This task replaces the static whitelist with mechanical discovery
over DOCS_DIR/**.md while preserving the compatibility contract:

* flat slugs (path parts joined with "-", relative to DOCS_DIR)
* established slugs keep resolving (override map for deviating files)
* legacy aliases keep resolving (bookmarks, dashboard links, node-cli docs)
* GET /relay/v2/docs index shape: {"docs": [{name, title, url, available}]}
* no dotfiles, no traversal, non-.md files are never servable
* unknown slugs still return 404

Red expectations on pre-T-216 code: the discovery-specific tests fail
(unknown discovered slugs 404 while ALLOWED_DOCS is still static). The
compatibility tests (dashboard slugs, legacy aliases, index shape) pass
on the old code and must stay green across the rewrite.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("RELAY_DB_PATH", "")

from fastapi.testclient import TestClient  # noqa: E402

from relay_server.api.v2 import docs as docs_mod  # noqa: E402
from relay_server.main import app  # noqa: E402

client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)

BASE = "/relay/v2/docs"

# Pages that ARE in the repo but were unreachable under the whitelist (F1).
FORMERLY_UNSERVED = [
    "server-docker",
    "node-node-daemon",
    "node-federation",
    "node-handler-contract",
    "node-handler-primitives",
    "node-hermes-integration",
    "node-ssn",
    "storage-storage",
    "storage-qnap-storage-node",
    "node-setup",  # served before, but must survive via discovery too
]

# Slugs the dashboard static pages link to (F8) — must never break.
DASHBOARD_SLUGS = [
    "concepts",
    "node-capabilities",
    "node-cli-reference",
    "node-config",
    "node-setup",
    "reference-api",
]

# Established primary slugs whose mechanical name deviates (override map).
ESTABLISHED_OVERRIDES = {
    "node-ha": "node/ha-node.md",
    "node-config": "node/node-config.md",
}

LEGACY_ALIASES = ["setup", "admin-setup", "dashboard", "node-readme",
                  "nodes-design", "token-concept", "token-lifecycle",
                  "capabilities", "design-board", "proxmox-worker-setup",
                  "api-reference"]


# --------------------------------------------------------------------------
# Slug rule (unit level, isolated via tmp docs dir)
# --------------------------------------------------------------------------

def test_slug_rule_joins_path_parts_with_dashes(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    (docs / "server").mkdir(parents=True)
    (docs / "node" / "handlers").mkdir(parents=True)
    (docs / "server" / "setup.md").write_text("# s\n")
    (docs / "node" / "handlers" / "contract.md").write_text("# c\n")
    (docs / "concepts.md").write_text("# c\n")
    monkeypatch.setattr(docs_mod, "DOCS_DIR", docs)

    slugs = docs_mod._discover_docs()
    assert "server-setup" in slugs
    assert "node-handlers-contract" in slugs
    assert "concepts" in slugs


def test_discovery_skips_dotfiles_and_non_md(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    (docs / ".hidden").mkdir(parents=True)
    (docs / ".hidden" / "secret.md").write_text("# s\n")
    (docs / "notes.txt").write_text("x")
    (docs / ".hidden-notes.md").write_text("# h\n")
    (docs / "visible.md").write_text("# v\n")
    monkeypatch.setattr(docs_mod, "DOCS_DIR", docs)

    slugs = docs_mod._discover_docs()
    assert set(slugs) == {"visible"}


# --------------------------------------------------------------------------
# Index endpoint
# --------------------------------------------------------------------------

def _index_names() -> set[str]:
    resp = client.get(BASE)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict) and "docs" in data
    items = data["docs"]
    assert isinstance(items, list) and items
    # Index shape contract (node-cli docs parses exactly this).
    for item in items:
        assert set(item) >= {"name", "title", "url", "available"}
        assert item["url"] == f"{BASE}/{item['name']}"
    return {item["name"] for item in items}


def test_index_lists_every_real_md_file():
    """Every real .md under docs/ is served — the 11 unreachable pages come back."""
    names = _index_names()
    expected = {
        "-".join(p.relative_to(docs_mod.DOCS_DIR).with_suffix("").parts)
        for p in docs_mod.DOCS_DIR.rglob("*.md")
        if not any(part.startswith(".") for part in p.relative_to(docs_mod.DOCS_DIR).parts)
    }
    missing = expected - names
    assert not missing, f"repo pages without a doc slug: {sorted(missing)}"


def test_index_includes_formerly_unserved_pages():
    names = _index_names()
    gone = [s for s in FORMERLY_UNSERVED if s not in names]
    assert not gone, f"formerly unserved pages still missing: {gone}"


def test_index_includes_project_root_extras():
    names = _index_names()
    for extra in ("readme", "changelog", "agent-readme"):
        assert extra in names, f"{extra} disappeared from the index"


def test_index_available_flag_matches_disk(tmp_path, monkeypatch):
    """Discovered slugs exist by construction (uncached discovery), so
    available is True for them; the flag stays meaningful for the static
    root extras (e.g. agent-readme when AGENT_README.md is absent)."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "present.md").write_text("# p\n")
    monkeypatch.setattr(docs_mod, "DOCS_DIR", docs)
    monkeypatch.setattr(
        docs_mod, "_ALLOW_EXTRAS",
        {"real-extra": tmp_path / "extra-root.md", "ghost-extra": tmp_path / "ghost.md"},
    )
    (tmp_path / "extra-root.md").write_text("# e\n")

    by_name = {i["name"]: i for i in client.get(BASE).json()["docs"]}
    assert by_name["present"]["available"] is True
    assert by_name["real-extra"]["available"] is True
    assert by_name["ghost-extra"]["available"] is False


# --------------------------------------------------------------------------
# Page resolution — compatibility contract
# --------------------------------------------------------------------------

@pytest.mark.parametrize("slug", DASHBOARD_SLUGS)
def test_dashboard_slugs_still_resolve(slug):
    assert client.get(f"{BASE}/{slug}").status_code == 200


@pytest.mark.parametrize("alias", LEGACY_ALIASES)
def test_legacy_aliases_still_resolve(alias):
    assert client.get(f"{BASE}/{alias}").status_code == 200


@pytest.mark.parametrize("slug,rel", sorted(ESTABLISHED_OVERRIDES.items()))
def test_established_slugs_with_deviating_paths_resolve(slug, rel):
    """node-ha / node-config keep their short slugs despite deeper paths."""
    target = docs_mod.DOCS_DIR / rel
    if not target.exists():
        pytest.skip(f"{rel} not in this docs checkout")
    assert client.get(f"{BASE}/{slug}").status_code == 200


def test_formerly_unserved_page_renders():
    resp = client.get(f"{BASE}/server-docker")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


# --------------------------------------------------------------------------
# Security / 404 behaviour (must not regress)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../../etc/passwd", "..", "unknown-page",
                                 "concepts.md.txt", "%2e%2e%2fetc"])
def test_unknown_or_malicious_slugs_404(bad):
    assert client.get(f"{BASE}/{bad}").status_code == 404


def test_single_dot_normalizes_to_index_pre_router():
    """/docs/. is path-normalized by ASGI to /docs (index) before the router
    sees it — standard HTTP behaviour, not an endpoint concern. The endpoint
    contract only requires: no traversal, no dotfiles, unknown -> 404."""
    resp = client.get(f"{BASE}/.")
    assert resp.status_code == 200


def test_discovery_never_lists_dotfiles_via_http(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    (docs / ".secret").mkdir(parents=True)
    (docs / ".secret" / "hidden.md").write_text("# h\n")
    monkeypatch.setattr(docs_mod, "DOCS_DIR", docs)
    monkeypatch.setattr(docs_mod, "_ALLOW_EXTRAS", {})

    names = {i["name"] for i in client.get(BASE).json()["docs"]}
    assert "secret-hidden" not in names and "-secret-hidden" not in names
    assert client.get(f"{BASE}/secret-hidden").status_code == 404