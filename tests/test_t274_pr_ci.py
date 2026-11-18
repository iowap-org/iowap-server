"""P3: workflow file for the PR test obligation was created and is usable.

Lightweight test: it verifies the on-disk artifact without executing GitHub
Actions — parse the YAML and check for the required contract elements
(trigger types, Python version, dependency installation and the exact pytest
invocation form used everywhere in the repo). This will turn red immediately
if the file disappears or the contract lines no longer match.
"""

import os
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pr-tests.yml"


def test_pr_tests_workflow_exists_and_is_valid_yaml():
    os.environ["RELAY_DB_PATH"] = ""
    assert WORKFLOW.is_file(), f"missing workflow file: {WORKFLOW}"
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    assert _trigger(parsed), "workflow must declare triggers"


def _trigger(parsed: dict) -> dict:
    # YAML 1.1 quirk: pyyaml parses the bare `on:` key as boolean True —
    # workflow consumers have to accept both spellings.
    return parsed.get("on") or parsed.get(True) or {}


def test_pr_tests_workflow_triggers_match_contract():
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = _trigger(parsed)
    assert "pull_request" in triggers, "must run on pull_request"
    assert set(triggers["pull_request"]["types"]) == {
        "opened", "synchronize", "reopened",
    }
    # T-274 P3: manual runs for backfills / dry-runs on demand.
    assert "workflow_dispatch" in triggers


def test_pr_tests_workflow_runs_the_documented_pytest_invocation():
    text = WORKFLOW.read_text(encoding="utf-8")
    # The repo's single test invocation: hermetic against PYTHONPATH
    # pollution, via the .venv — same command as local/CT-deploy.
    assert "env -u PYTHONPATH .venv/bin/python -m pytest" in text
    # Same Python version as publish.yml (no matrix — T-266 decision).
    assert 'python-version: "3.11"' in text
    # Package + dev deps installed so the suite imports relay_server.
    assert '-e ".[dev]"' in text


def test_pr_tests_workflow_has_concurrency_group():
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    concurrency = parsed.get("concurrency") or {}
    assert concurrency.get("group"), "concurrency group required"
    # A superseded commit of the same PR does not need to keep running.
    assert concurrency.get("cancel-in-progress") is True
