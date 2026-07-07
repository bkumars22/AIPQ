"""Every 15 minutes: check drift on every deployed prompt version, roll back on CRITICAL."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import get_pool
from detectors.drift_detector import DriftDetector, FEATURE_NAMES, RollbackEngine

logger = logging.getLogger("aipq.scheduler")

_detector = DriftDetector()


async def monitor_all_deployed_versions() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        deployed = await conn.fetch("SELECT id, prompt_id FROM prompt_versions WHERE status = 'DEPLOYED'")

    for row in deployed:
        version_id, prompt_id = row["id"], row["prompt_id"]

        async with pool.acquire() as conn:
            latest = await conn.fetchrow(
                f"""
                SELECT {', '.join(FEATURE_NAMES)}, recorded_at FROM drift_records
                WHERE prompt_version_id = $1 ORDER BY recorded_at DESC LIMIT 1
                """,
                version_id,
            )
        if latest is None:
            continue

        result = await _detector.score_new_sample(version_id, dict(latest))

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE drift_records SET is_anomaly = $1, drift_severity = $2
                WHERE prompt_version_id = $3 AND recorded_at = $4
                """,
                result.is_anomaly, result.drift_severity, version_id, latest["recorded_at"],
            )

        if result.is_anomaly:
            logger.warning("Drift detected on version %d: %s (%s)", version_id, result.drift_severity, result.explanation)
            await RollbackEngine.rollback_if_critical(prompt_id, result)


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(monitor_all_deployed_versions, "interval", minutes=15, id="aipq_drift_monitor")
    scheduler.start()
    logger.info("Drift monitoring scheduler started (every 15 minutes)")
    return scheduler
