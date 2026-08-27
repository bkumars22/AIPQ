"""
Tests for the human-in-the-loop borderline review feature added on top of
evaluators/pipeline.py's existing 5-node graph.

Two tiers:

1. Pure-logic + mocked-DB unit tests (TestRouting, TestAutoPaths,
   TestBorderlineHumanReview) — no real Postgres needed, run in any
   environment.

2. TestProcessRestartDurability — the single most important test per the
   feature's own design brief: it must prove resume_review() works from
   only a thread_id, using a completely separate AsyncPostgresSaver +
   compiled graph instance than the one that paused (the closest a
   same-process test can get to an actual process kill/restart without
   literally forking a subprocess). This requires a real, reachable
   Postgres (AIPQ_DATABASE_URL) with a working psycopg driver — it skips
   itself (not a failure) when either isn't available, e.g. no libpq
   installed, or no local Postgres running.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evaluators.pipeline import (  # noqa: E402
    auto_approve_node,
    auto_reject_node,
    resume_review,
    route_after_aggregate,
    run_evaluation,
)


def _postgres_checkpointer_available() -> bool:
    """True only if psycopg can actually connect to AIPQ_DATABASE_URL right now."""
    try:
        import asyncio

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        dsn = os.getenv("AIPQ_DATABASE_URL", "postgresql://aipq:aipq_local_2026@localhost:5433/aipq")

        async def _probe():
            async with AsyncPostgresSaver.from_conn_string(dsn) as cp:
                await cp.setup()

        asyncio.run(_probe())
        return True
    except Exception:
        return False


def _state(overall_score: float, lower=None, upper=None, error="") -> dict:
    return {
        "error": error,
        "borderline_lower": lower,
        "borderline_upper": upper,
        "evaluation_summary": {"overall_score": overall_score, "previous_score": 0.85, "passed": overall_score >= 0.85},
    }


class TestRouting:
    def test_no_borderline_config_goes_straight_to_store_and_decide(self):
        # Pre-existing datasets have NULL bounds -- must be a total no-op.
        assert route_after_aggregate(_state(0.60)) == "store_and_decide"
        assert route_after_aggregate(_state(0.95)) == "store_and_decide"

    def test_error_state_goes_straight_to_store_and_decide(self):
        assert route_after_aggregate(_state(0.82, lower=0.75, upper=0.90, error="boom")) == "store_and_decide"

    def test_below_lower_bound_auto_rejects(self):
        assert route_after_aggregate(_state(0.60, lower=0.75, upper=0.90)) == "auto_reject"

    def test_at_or_above_upper_bound_auto_approves(self):
        assert route_after_aggregate(_state(0.90, lower=0.75, upper=0.90)) == "auto_approve"
        assert route_after_aggregate(_state(0.97, lower=0.75, upper=0.90)) == "auto_approve"

    def test_between_bounds_is_human_review(self):
        assert route_after_aggregate(_state(0.82, lower=0.75, upper=0.90)) == "human_review"


class TestAutoPaths:
    @pytest.mark.asyncio
    async def test_auto_reject_forces_passed_false(self):
        state = _state(0.60, lower=0.75, upper=0.90)  # would be "passed": False already, but must be explicit
        result = await auto_reject_node(state)
        assert result["evaluation_summary"]["passed"] is False
        assert result["review_status"] == "auto_rejected"

    @pytest.mark.asyncio
    async def test_auto_approve_forces_passed_true_even_if_below_dataset_threshold(self):
        # score 0.90 >= borderline_upper -> auto-approved, regardless of the
        # separate (lower) golden_dataset.threshold used by the pre-existing
        # calculate_aggregate "passed" computation.
        state = _state(0.90, lower=0.75, upper=0.90)
        result = await auto_approve_node(state)
        assert result["evaluation_summary"]["passed"] is True
        assert result["review_status"] == "auto_approved"


def _mock_redis():
    redis = AsyncMock()
    redis.get.return_value = None  # always a cache miss
    return redis


def _mock_pool(fetchrow_results=None, fetch_results=None):
    mock_conn = AsyncMock()
    if fetchrow_results is not None:
        mock_conn.fetchrow.side_effect = fetchrow_results
    if fetch_results is not None:
        mock_conn.fetch.side_effect = fetch_results
    # conn.transaction() is a plain (sync) call in asyncpg that returns an
    # async context manager -- AsyncMock would otherwise make the call
    # itself a coroutine, which breaks `async with conn.transaction():`.
    mock_conn.transaction = MagicMock()
    mock_conn.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
    mock_conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool.acquire.return_value.__aexit__.return_value = None
    return mock_pool, mock_conn


class TestBorderlineHumanReviewEndToEnd:
    """
    Exercises the full graph for all three paths through run_evaluation(),
    with the *business* DB (prompts/golden_datasets/prompt_versions/
    evaluations) mocked exactly like the rest of this test suite always has
    -- but using LangGraph's real in-memory checkpointer (not Postgres) so
    the graph's interrupt/pause mechanics themselves are genuinely
    exercised, not mocked away. Postgres-specific durability is
    TestProcessRestartDurability's job, below.
    """

    def _rows(self, score: float, lower, upper):
        version_row = {"prompt_id": 1, "content": "system prompt text"}
        dataset_row = {
            "id": 1, "threshold": 0.85,
            "borderline_lower": lower, "borderline_upper": upper, "review_timeout_hours": 48.0,
        }
        case_rows = [{
            "id": 1, "input_text": "hi", "expected_behavior": "be nice",
            "forbidden_patterns": [], "required_patterns": [], "category": "baseline",
        }]
        prev_score_row = {"quality_score": 0.85}
        return version_row, dataset_row, case_rows, prev_score_row, score

    async def _run_with_score(self, score: float, lower, upper, checkpointer):
        version_row, dataset_row, case_rows, prev_score_row, _ = self._rows(score, lower, upper)
        mock_pool, mock_conn = _mock_pool(
            fetchrow_results=[version_row, dataset_row, prev_score_row],
            fetch_results=[case_rows],
        )
        mock_geval = MagicMock()
        mock_geval.score = score

        with patch("evaluators.pipeline.get_pool", new_callable=AsyncMock, return_value=mock_pool), \
             patch("evaluators.pipeline.get_redis", return_value=_mock_redis()), \
             patch("evaluators.pipeline.run_prompt_under_test", new_callable=AsyncMock, return_value="a fine response"), \
             patch("deepeval.metrics.GEval", return_value=mock_geval), \
             patch("evaluators.pipeline._checkpointer_dsn", return_value="unused"):

            from langgraph.checkpoint.memory import InMemorySaver

            from evaluators.pipeline import _initial_state, build_graph

            cp = checkpointer or InMemorySaver()
            thread_id = "aipq-test-1-999"
            config = {"configurable": {"thread_id": thread_id}}
            graph = build_graph(checkpointer=cp)
            initial = _initial_state(999, 1, 1, 0.85, thread_id)
            result = await graph.ainvoke(initial, config)
            return result, cp, thread_id, mock_conn

    @pytest.mark.asyncio
    async def test_clearly_bad_score_auto_rejects_no_human_needed(self):
        result, _, _, mock_conn = await self._run_with_score(0.60, lower=0.75, upper=0.90, checkpointer=None)
        assert "__interrupt__" not in result
        assert result["review_status"] == "auto_rejected"
        assert result["evaluation_summary"]["passed"] is False

    @pytest.mark.asyncio
    async def test_clearly_good_score_auto_approves_no_human_needed(self):
        result, _, _, mock_conn = await self._run_with_score(0.93, lower=0.75, upper=0.90, checkpointer=None)
        assert "__interrupt__" not in result
        assert result["review_status"] == "auto_approved"
        assert result["evaluation_summary"]["passed"] is True

    @pytest.mark.asyncio
    async def test_borderline_score_pauses_for_human_review(self):
        result, _, _, mock_conn = await self._run_with_score(0.82, lower=0.75, upper=0.90, checkpointer=None)
        assert "__interrupt__" in result
        # pending_reviews INSERT must have been attempted
        insert_calls = [c for c in mock_conn.execute.call_args_list if "INSERT INTO pending_reviews" in c.args[0]]
        assert len(insert_calls) == 1

    @pytest.mark.asyncio
    async def test_borderline_then_human_approves(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Command

        cp = InMemorySaver()
        result, cp, thread_id, mock_conn = await self._run_with_score(0.82, lower=0.75, upper=0.90, checkpointer=cp)
        assert "__interrupt__" in result

        with patch("evaluators.pipeline.get_pool", new_callable=AsyncMock, return_value=_mock_pool()[0]):
            from evaluators.pipeline import build_graph
            graph = build_graph(checkpointer=cp)
            config = {"configurable": {"thread_id": thread_id}}
            resumed = await graph.ainvoke(Command(resume="approve"), config)

        assert resumed["evaluation_summary"]["passed"] is True
        assert resumed["review_status"] == "approved_by_human"
        assert resumed["human_decision"] == "approve"

    @pytest.mark.asyncio
    async def test_borderline_then_human_rejects(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Command

        cp = InMemorySaver()
        result, cp, thread_id, mock_conn = await self._run_with_score(0.82, lower=0.75, upper=0.90, checkpointer=cp)
        assert "__interrupt__" in result

        with patch("evaluators.pipeline.get_pool", new_callable=AsyncMock, return_value=_mock_pool()[0]):
            from evaluators.pipeline import build_graph
            graph = build_graph(checkpointer=cp)
            config = {"configurable": {"thread_id": thread_id}}
            resumed = await graph.ainvoke(Command(resume="reject"), config)

        assert resumed["evaluation_summary"]["passed"] is False
        assert resumed["review_status"] == "rejected_by_human"


@pytest.mark.skipif(
    not _postgres_checkpointer_available(),
    reason="requires a real, reachable Postgres at AIPQ_DATABASE_URL with a working psycopg driver",
)
class TestProcessRestartDurability:
    """
    The single most important test for this feature (see the feature's own
    design brief). Deliberately does NOT share the checkpointer, the
    compiled graph, or any Python object between the pause and the resume
    -- each is built fresh from nothing but the thread_id and the DSN,
    exactly as a genuinely separate process would have to.
    """

    @pytest.mark.asyncio
    async def test_resume_after_simulated_restart_using_only_thread_id(self):
        version_row = {"prompt_id": 1, "content": "system prompt text"}
        dataset_row = {
            "id": 1, "threshold": 0.85,
            "borderline_lower": 0.75, "borderline_upper": 0.90, "review_timeout_hours": 48.0,
        }
        case_rows = [{
            "id": 1, "input_text": "hi", "expected_behavior": "be nice",
            "forbidden_patterns": [], "required_patterns": [], "category": "baseline",
        }]
        prev_score_row = {"quality_score": 0.85}
        mock_pool_1, _ = _mock_pool(
            fetchrow_results=[version_row, dataset_row, prev_score_row], fetch_results=[case_rows],
        )
        mock_geval = MagicMock()
        mock_geval.score = 0.82

        with patch("evaluators.pipeline.get_pool", new_callable=AsyncMock, return_value=mock_pool_1), \
             patch("evaluators.pipeline.get_redis", return_value=_mock_redis()), \
             patch("evaluators.pipeline.run_prompt_under_test", new_callable=AsyncMock, return_value="a fine response"), \
             patch("deepeval.metrics.GEval", return_value=mock_geval):
            result = await run_evaluation(version_id=999999, prompt_id=888888, golden_dataset_id=1, threshold=0.85)

        assert "__interrupt__" in result, "expected the graph to pause for human review"
        thread_id = "aipq-888888-999999"

        # --- simulated restart: no object above this line is referenced below ---

        mock_pool_2, _ = _mock_pool()  # a brand-new mock -- resume_review touches no business tables at all
        with patch("evaluators.pipeline.get_pool", new_callable=AsyncMock, return_value=mock_pool_2):
            resumed = await resume_review(thread_id, "approve")

        assert resumed["evaluation_summary"]["passed"] is True
        assert resumed["review_status"] == "approved_by_human"
        assert resumed["human_decision"] == "approve"
