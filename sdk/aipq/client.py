"""
AIPQ Python SDK — version control, quality gating, and drift reporting for
prompts in any AI project, with a single decorator.

Usage:

    from aipq import AIPQClient, aipq_prompt

    aipq = AIPQClient(
        api_key=os.getenv("AIPQ_API_KEY"),
        project_id=os.getenv("AIPQ_PROJECT_ID"),
        base_url="https://your-aipq.railway.app",
    )

    @aipq_prompt(name="aria_socratic_system", dataset="aria_adversarial_golden", threshold=0.90)
    async def get_system_prompt() -> str:
        return "You are ARIA — a Socratic AI tutor. RULE 1: NEVER give direct answers."

    # Elsewhere, after generating a response:
    asyncio.create_task(
        aipq.report_usage(
            prompt_name="aria_socratic_system",
            output=response_text,
            context=retrieved_context,
            quality_score=measured_compliance,
        )
    )

Design notes:
- Version-creation/evaluation calls (the quality gate) RAISE on failure —
  that's the safety-critical path and must never be silently swallowed.
- Telemetry calls (report_usage, create_golden_case) SILENTLY FAIL on
  network errors — a down AIPQ instance must never break the calling app.
- @aipq_prompt binds to the most recently constructed AIPQClient unless a
  client is passed explicitly, matching the single-client-per-process usage
  shown above.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

from .exceptions import AIPQError, PromptQualityError

logger = logging.getLogger("aipq.sdk")

_DEFAULT_TIMEOUT = 10.0
_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 0.5

# The most recently constructed AIPQClient — @aipq_prompt uses this when no
# client is passed explicitly, so decorators can be declared without wiring
# the client through every function signature.
_default_client: Optional["AIPQClient"] = None


class AIPQClient:
    def __init__(self, api_key: str, project_id: str, base_url: str = "http://localhost:8001"):
        if not api_key:
            raise ValueError("AIPQClient requires api_key")
        if not project_id:
            raise ValueError("AIPQClient requires project_id")

        self.api_key = api_key
        self.project_id = project_id
        self.base_url = base_url.rstrip("/")
        self._session = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_DEFAULT_TIMEOUT,
        )
        # prompt_name -> prompt_id, resolved lazily on first use
        self._prompt_id_cache: dict[str, int] = {}
        # prompt_name -> currently deployed prompt_version.id (drift_records is keyed by
        # version, not prompt — this must never be confused with _prompt_id_cache above)
        self._version_id_cache: dict[str, int] = {}

        global _default_client
        _default_client = self

    async def aclose(self) -> None:
        await self._session.aclose()

    # ── internal HTTP helpers ────────────────────────────────────────────

    async def _request(
        self, method: str, path: str, *, critical: bool, treat_404_as_none: bool = False, **kwargs
    ) -> Optional[dict]:
        """
        Make an HTTP call with retries.

        critical=True  -> re-raises AIPQError after retries are exhausted
                           (used for the quality-gate path).
        critical=False -> logs and returns None after retries are exhausted
                           (used for telemetry — never breaks the caller).
        treat_404_as_none=True -> a 404 is a normal, expected outcome (e.g. no
                           deployed version yet) — return None immediately,
                           no retries, no error log.
        """
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = await self._session.request(method, path, **kwargs)
                if treat_404_as_none and resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json() if resp.content else {}
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                logger.warning(
                    "AIPQ request %s %s failed (attempt %d/%d): %s",
                    method, path, attempt, _MAX_RETRIES, exc,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS * attempt)

        if critical:
            raise AIPQError(f"AIPQ request {method} {path} failed after {_MAX_RETRIES} attempts: {last_exc}")
        logger.error("AIPQ telemetry call %s %s dropped (AIPQ unreachable): %s", method, path, last_exc)
        return None

    async def _ensure_prompt_registered(self, prompt_name: str, dataset: str, threshold: float) -> int:
        if prompt_name in self._prompt_id_cache:
            return self._prompt_id_cache[prompt_name]

        result = await self._request(
            "POST", "/prompts/register", critical=True,
            json={
                "project_id": self.project_id,
                "prompt_name": prompt_name,
                "golden_dataset": dataset,
                "threshold": threshold,
            },
        )
        prompt_id = result["prompt_id"]
        self._prompt_id_cache[prompt_name] = prompt_id
        return prompt_id

    # ── public API ────────────────────────────────────────────────────────

    async def get_current_version(self, prompt_name: str) -> Optional[dict]:
        """Return the currently deployed version's payload, or None if none deployed yet."""
        prompt_id = self._prompt_id_cache.get(prompt_name)
        if prompt_id is None:
            return None
        result = await self._request(
            "GET", f"/prompts/{prompt_id}/current", critical=True, treat_404_as_none=True
        )
        if result is not None:
            self._version_id_cache[prompt_name] = result["id"]
        return result

    async def create_version(
        self,
        prompt_name: str,
        content: str,
        dataset: str,
        threshold: float,
        changed_by: str = "sdk",
        change_message: str = "Auto-versioned by SDK",
    ) -> dict:
        """
        Register a new prompt version and wait for its quality gate to resolve.

        Version creation returns immediately with status=TESTING (evaluation
        runs in the background on the ai-engine side, per AIPQ's backend
        design) — this method polls until the version leaves TESTING, then
        raises PromptQualityError if it landed on FAILED.
        """
        prompt_id = await self._ensure_prompt_registered(prompt_name, dataset, threshold)
        created = await self._request(
            "POST", "/prompts/versions", critical=True,
            json={
                "prompt_id": prompt_id,
                "content": content,
                "dataset": dataset,
                "threshold": threshold,
                "changed_by": changed_by,
                "change_message": change_message,
            },
        )
        version_number = created["version_number"]
        resolved = await self._poll_version_resolved(prompt_id, version_number)

        if resolved["status"] != "DEPLOYED":
            raise PromptQualityError(
                prompt_name=prompt_name,
                score=resolved.get("quality_score") or 0.0,
                threshold=threshold,
                details=resolved,
            )
        self._version_id_cache[prompt_name] = resolved["id"]
        return {**resolved, "content": content}

    async def _poll_version_resolved(
        self, prompt_id: int, version_number: int,
        timeout_seconds: float = 60.0, interval_seconds: float = 1.5,
    ) -> dict:
        """Poll /prompts/{id}/versions until the given version leaves TESTING."""
        elapsed = 0.0
        while elapsed < timeout_seconds:
            result = await self._request("GET", f"/prompts/{prompt_id}/versions", critical=True)
            match = next((v for v in result.get("versions", []) if v["version_number"] == version_number), None)
            if match is not None and match["status"] != "TESTING":
                return match
            await asyncio.sleep(interval_seconds)
            elapsed += interval_seconds

        raise AIPQError(
            f"Evaluation for prompt_id={prompt_id} version={version_number} did not resolve "
            f"within {timeout_seconds}s — ai-engine may be down or overloaded"
        )

    async def report_usage(
        self,
        prompt_name: str,
        output: str,
        context: str = "",
        quality_score: Optional[float] = None,
    ) -> None:
        """
        Report one real usage of a prompt for drift monitoring.

        Never raises — silently drops the sample if AIPQ is unreachable so
        the calling application is never affected by AIPQ downtime.
        """
        version_id = self._version_id_cache.get(prompt_name)
        if version_id is None:
            logger.debug(
                "report_usage called before prompt '%s' had a resolved deployed version — skipping",
                prompt_name,
            )
            return
        await self._request(
            "POST", "/drift/record", critical=False,
            json={
                "prompt_version_id": version_id,
                "output": output,
                "context": context,
                "quality_score": quality_score,
            },
        )

    async def get_best_version(self, prompt_name: str) -> Optional[str]:
        """Return the content of the highest-scoring version of this prompt (for rollback suggestions)."""
        prompt_id = self._prompt_id_cache.get(prompt_name)
        if prompt_id is None:
            return None
        result = await self._request("GET", f"/prompts/{prompt_id}/versions", critical=True)
        versions = result.get("versions", []) if result else []
        if not versions:
            return None
        best = max(versions, key=lambda v: v.get("quality_score") or 0.0)
        return best.get("content")

    async def create_golden_case(
        self,
        prompt_name: str,
        input_text: str,
        expected_behavior: str,
        forbidden: Optional[list[str]] = None,
        required: Optional[list[str]] = None,
        category: str = "baseline",
    ) -> None:
        """
        Add a test case to the prompt's golden dataset — call this whenever
        you find a new failure mode, to prevent it from regressing forever.

        Silently fails if AIPQ is unreachable (non-critical telemetry-like path).
        """
        prompt_id = self._prompt_id_cache.get(prompt_name)
        if prompt_id is None:
            logger.warning("create_golden_case called before prompt '%s' was registered — skipping", prompt_name)
            return
        await self._request(
            "POST", "/golden-cases", critical=False,
            json={
                "prompt_id": prompt_id,
                "input_text": input_text,
                "expected_behavior": expected_behavior,
                "forbidden_patterns": forbidden or [],
                "required_patterns": required or [],
                "category": category,
            },
        )


def aipq_prompt(
    name: str,
    dataset: str,
    threshold: float = 0.85,
    client: Optional[AIPQClient] = None,
    changed_by: str = "sdk",
):
    """
    Decorate an async function that returns prompt text. On every call:

    1. Calls the wrapped function to get the current prompt text.
    2. Compares it against the last deployed version.
    3. If changed: creates a new version and runs it through the golden
       dataset. A passing score deploys it; a failing score raises
       PromptQualityError and the returned text is never used.
    4. If unchanged: returns the already-deployed text (no extra evaluation).

    Raises PromptQualityError (uncaught) if a changed prompt fails its
    quality gate — this is intentional: it should block your application
    from starting with an unvalidated prompt, the same way a failing
    migration blocks a deploy.

    Distinct from that: if AIPQ itself is unreachable (network error, AIPQ
    down), this fails OPEN — logs a warning and returns the raw prompt text
    unvalidated, rather than blocking your application on an AIPQ outage.
    Only a genuine quality-gate failure (AIPQ reachable, evaluation ran,
    score below threshold) raises.
    """
    def decorator(fn: Callable[[], Awaitable[str]]):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> str:
            active_client = client or _default_client
            if active_client is None:
                raise AIPQError(
                    "No AIPQClient available — construct one before using @aipq_prompt, "
                    "or pass client=... explicitly."
                )

            text = await fn(*args, **kwargs)

            try:
                current = await active_client.get_current_version(name)

                if current is not None and current.get("content") == text:
                    return current["content"]

                result = await active_client.create_version(
                    prompt_name=name,
                    content=text,
                    dataset=dataset,
                    threshold=threshold,
                    changed_by=changed_by,
                    change_message="Auto-versioned by @aipq_prompt (content changed)",
                )
                return result.get("content", text)
            except PromptQualityError:
                raise
            except AIPQError as exc:
                logger.warning(
                    "AIPQ unreachable while versioning prompt '%s' — failing open, "
                    "returning unvalidated prompt text: %s", name, exc,
                )
                return text

        return wrapper

    return decorator
