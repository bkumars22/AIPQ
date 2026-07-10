#!/usr/bin/env python3
"""
Script backing the aipq-evaluate GitHub Action.

Self-contained (stdlib + requests only — no AIPQ SDK import) so it behaves
identically regardless of which repo/ref checks it out and invokes it;
importing the SDK package here would tie this action to AIPQ's own repo
layout, which breaks the moment a consumer repo (e.g. ARIA) uses this
action without also vendoring AIPQ's sdk/ directory.
"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

API_URL = os.environ["AIPQ_API_URL"].rstrip("/")
API_KEY = os.environ["AIPQ_API_KEY"]
PROJECT_ID = int(os.environ["AIPQ_PROJECT_ID"])
PROMPT_NAME = os.environ["AIPQ_PROMPT_NAME"]
PROMPT_FILE = os.environ["AIPQ_PROMPT_FILE"]
DATASET = os.environ["AIPQ_DATASET"]
THRESHOLD = float(os.environ.get("AIPQ_THRESHOLD", "0.85"))
POLL_TIMEOUT = float(os.environ.get("AIPQ_POLL_TIMEOUT", "60"))
GITHUB_TOKEN = os.environ.get("AIPQ_GITHUB_TOKEN")

HEADERS = {"Authorization": f"Bearer {API_KEY}"}
GITHUB_OUTPUT = os.environ.get("GITHUB_OUTPUT")


def _set_output(name: str, value: str) -> None:
    if GITHUB_OUTPUT:
        with open(GITHUB_OUTPUT, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def _fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def register_prompt() -> int:
    resp = requests.post(
        f"{API_URL}/prompts/register", headers=HEADERS,
        json={
            "project_id": PROJECT_ID, "prompt_name": PROMPT_NAME,
            "golden_dataset": DATASET, "threshold": THRESHOLD,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["prompt_id"]


def get_current_content(prompt_id: int) -> str | None:
    resp = requests.get(f"{API_URL}/prompts/{prompt_id}/current", headers=HEADERS, timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["content"]


def create_version(prompt_id: int, content: str) -> dict:
    resp = requests.post(
        f"{API_URL}/prompts/versions", headers=HEADERS,
        json={
            "prompt_id": prompt_id, "content": content, "dataset": DATASET, "threshold": THRESHOLD,
            "changed_by": "github-action", "change_message": f"CI run {os.environ.get('GITHUB_RUN_ID', '')}",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def poll_resolved(prompt_id: int, version_number: int) -> dict:
    elapsed = 0.0
    interval = 2.0
    while elapsed < POLL_TIMEOUT:
        resp = requests.get(f"{API_URL}/prompts/{prompt_id}/versions", headers=HEADERS, timeout=10)
        resp.raise_for_status()
        versions = resp.json().get("versions", [])
        match = next((v for v in versions if v["version_number"] == version_number), None)
        if match is not None and match["status"] != "TESTING":
            return match
        time.sleep(interval)
        elapsed += interval
    _fail(f"Evaluation did not resolve within {POLL_TIMEOUT}s — ai-engine may be down or overloaded")


def post_pr_comment(body: str) -> None:
    """Best-effort: silently does nothing outside a pull_request event, or
    if no token was supplied — a broken PR comment must never fail the job
    on its own (the quality gate's pass/fail exit code is what matters)."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (event_path and repo and GITHUB_TOKEN):
        return
    try:
        with open(event_path, encoding="utf-8") as f:
            event = json.load(f)
        pr_number = event.get("pull_request", {}).get("number") or event.get("number")
        if not pr_number:
            return
        requests.post(
            f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"body": body},
            timeout=10,
        )
    except (OSError, json.JSONDecodeError, requests.RequestException) as exc:
        print(f"::warning::Could not post PR comment: {exc}")


def main() -> None:
    if not os.path.exists(PROMPT_FILE):
        _fail(f"prompt-file not found: {PROMPT_FILE}")
    with open(PROMPT_FILE, encoding="utf-8") as f:
        local_content = f.read()

    prompt_id = register_prompt()
    current_content = get_current_content(prompt_id)

    if current_content == local_content:
        print(f"AIPQ: '{PROMPT_NAME}' content unchanged — skipping evaluation.")
        _set_output("status", "UNCHANGED")
        return

    created = create_version(prompt_id, local_content)
    resolved = poll_resolved(prompt_id, created["version_number"])

    score = resolved.get("quality_score")
    status = resolved["status"]
    score_str = f"{score:.2f}" if score is not None else "n/a"
    _set_output("quality-score", score_str)
    _set_output("status", status)

    if status == "DEPLOYED":
        body = (
            f"✅ **AIPQ prompt quality: {score_str}** (threshold: {THRESHOLD}) "
            f"— `{PROMPT_NAME}` v{created['version_number']} deployed."
        )
        print(body)
        post_pr_comment(body)
    else:
        body = (
            f"❌ **AIPQ prompt quality: {score_str}** (threshold: {THRESHOLD}) "
            f"— `{PROMPT_NAME}` v{created['version_number']} blocked (status: {status}).\n\n"
            f"Deployment blocked — see the AIPQ dashboard for this prompt's version history."
        )
        print(body)
        post_pr_comment(body)
        _fail(f"Prompt quality {score_str} did not clear threshold {THRESHOLD} — deployment blocked.")


if __name__ == "__main__":
    main()
