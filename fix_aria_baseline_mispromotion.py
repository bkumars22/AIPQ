"""
Corrects an accidental A/B-test promotion on aria_socratic_system_baseline
(prompt_id=3), unrelated to today's real ARIA/QAIP adversarial-prompt fix.

Real events, confirmed from backend access logs (172.19.0.1 = host/browser,
with an OPTIONS CORS preflight before each POST -- a real browser, not a
script or curl):
  04:19:20 POST /ab-tests                    (created test id=1, A=v31, B=v32)
  04:19:23 POST /ab-tests/1/promote?version=A (promoted v31 -- the WORSE,
                                                original FAILED/score-0 version
                                                from before any golden cases
                                                existed -- over v32, the real
                                                DEPLOYED/score-1.0 version)

Root cause: almost certainly a stray coordinate-based click from this
session's own browser automation landing on the wrong element while taking
follow-up screenshots on that page (a real, correct screenshot of v2/1.00
was captured at 04:14:56, ~4.5 minutes before this) -- not anything the
dashboard, backend, or scheduler did on its own, and not a deliberate
action. ABTestDetail.tsx's promote button only fires on an explicit
onClick, confirmed by reading the frontend source.

Fix: put prompt_id=3 back to what it legitimately was -- v32 (score 1.0)
DEPLOYED and current, v31 back to FAILED (what a version with zero golden
cases correctly resolves to). The ab_tests row itself is left in place,
not deleted -- it's a real event that really happened, even if accidental;
deleting it would be less honest than a status table correctly reflecting
what's actually deployed now.
"""
import asyncio
import os

import asyncpg

DB_URL = os.environ["AIPQ_DATABASE_URL"]


async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        before = await conn.fetch(
            "SELECT id, status, quality_score, deployed_at FROM prompt_versions WHERE id IN (31, 32) ORDER BY id"
        )
        print("Before:")
        for r in before:
            print(f"  v{r['id']}: status={r['status']} score={r['quality_score']} deployed_at={r['deployed_at']}")

        async with conn.transaction():
            await conn.execute(
                "UPDATE prompt_versions SET status = 'FAILED', deployed_at = NULL WHERE id = 31"
            )
            await conn.execute(
                "UPDATE prompt_versions SET status = 'DEPLOYED', deployed_at = now() WHERE id = 32"
            )
            await conn.execute(
                "UPDATE prompts SET current_version_id = 32 WHERE id = 3"
            )

        after = await conn.fetch(
            "SELECT id, status, quality_score, deployed_at FROM prompt_versions WHERE id IN (31, 32) ORDER BY id"
        )
        print("\nAfter:")
        for r in after:
            print(f"  v{r['id']}: status={r['status']} score={r['quality_score']} deployed_at={r['deployed_at']}")

        current = await conn.fetchval("SELECT current_version_id FROM prompts WHERE id = 3")
        print(f"\nprompts.current_version_id for prompt_id=3: {current} (should be 32)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
