"""
API key generation/hashing for machine callers (the SDK, the GitHub Action).

The raw key is shown to the caller exactly once, at /projects/register time.
Only its SHA-256 hash is ever stored (in projects.webhook_secret), so a
leaked database dump does not expose usable credentials — same treatment
as a password.
"""
from __future__ import annotations

import hashlib
import secrets


def generate_api_key() -> str:
    return f"aipq_{secrets.token_urlsafe(32)}"


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
