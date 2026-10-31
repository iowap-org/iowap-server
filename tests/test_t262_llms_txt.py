"""T-262: `llms.txt` delivery through the docs portal.

`GET /relay/v2/docs/llms.txt` returns the docs submodule's `llms.txt` file
RAW (text/plain — the agent bootstrap index must be machine-readable, not
HTML-rendered). The mechanical *.md discovery still never matches .txt
files; the file rides the `_ALLOW_EXTRAS` whitelist under its literal
slug. No root `/llms.txt` alias exists: the docs mount is prefix-bound
(api/v2/__init__.py, prefix="/docs" under /relay/v2) — documented in
iowap-docs reference/api.md (T-263).
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RELAY_DB_PATH", "")

from relay_server.api.v2 import docs as docs_mod  # noqa: E402
from relay_server.main import app  # noqa: E402

client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
BASE = "/relay/v2/docs"

REPO_LLMSTXT = docs_mod.DOCS_DIR / "llms.txt"


@pytest.fixture(autouse=True)
def _require_repo_llms_txt():
    """The llms.txt file lives in the docs SUBMODULE — skip loudly when the
    checkout did not run `git submodule update --init`."""
    if not REPO_LLMSTXT.exists():
        pytest.skip(
            "docs/llms.txt missing — the docs submodule is not checked out"
        )


def test_llms_txt_served_raw_and_matches_repo_file():
    resp = client.get(f"{BASE}/llms.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == REPO_LLMSTXT.read_text(encoding="utf-8"), (
        "the served llms.txt is byte-identical to the repo file"
    )


def test_llms_txt_not_html_rendered():
    """The md-renderer path must not catch the .txt extra (no <html> shell)."""
    resp = client.get(f"{BASE}/llms.txt")
    assert not resp.text.lstrip().startswith("<!DOCTYPE"), (
        "llms.txt must not be wrapped in the HTML page template"
    )


def test_llms_txt_indexed_as_extra():
    data = client.get(BASE).json()["docs"]
    by_name = {item["name"]: item for item in data}
    assert "llms.txt" in by_name
    assert by_name["llms.txt"]["url"] == f"{BASE}/llms.txt"
    assert by_name["llms.txt"]["available"] is True


def test_md_discovery_still_never_matches_txt(monkeypatch, tmp_path):
    """t216 contract stands: a .txt under docs/ is NOT mechanically
    discovered — llms.txt is only servable via the literal extra."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.txt").write_text("x\n")
    monkeypatch.setattr(docs_mod, "DOCS_DIR", docs)
    assert "notes.txt" not in docs_mod._discover_docs()


def test_llms_txt_ghost_extra_unavailable(tmp_path, monkeypatch):
    """When the submodule file is absent the index reports available: false
    and the page handler 404s (no crash)."""
    monkeypatch.setattr(
        docs_mod, "_ALLOW_EXTRAS", {"llms.txt": tmp_path / "missing-llms.txt"}
    )
    by_name = {i["name"]: i for i in client.get(BASE).json()["docs"]}
    assert by_name["llms.txt"]["available"] is False
    resp = client.get(f"{BASE}/llms.txt")
    assert resp.status_code == 404