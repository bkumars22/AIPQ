"""
Dedupe QAIP's scope dataset (dataset_id=2): the same MT-01 input got
re-added once per debugging run this session -- 7 identical rows for 1
unique input. Keeps id=4 (the first copy), deletes the other 6.

No rubric rewrite here -- QAIP's expected_behavior was already written as
a proper description of the required reply shape (stay in format, state
out of scope, never fabricate), unlike ARIA's 2 broken rubrics. QAIP's
real 0.06 score is a genuine behavioral gap (see fix_qaip_prompt.py),
not a rubric-wording bug.

Does not touch prompt_versions or evaluations tables.
"""
import asyncio
import os

import asyncpg

DB_URL = os.environ["AIPQ_DATABASE_URL"]


async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        before = await conn.fetch(
            "SELECT id, input_text FROM golden_cases WHERE dataset_id = 2 ORDER BY id"
        )
        print(f"Before: {len(before)} rows in dataset_id=2")

        async with conn.transaction():
            deleted = await conn.execute(
                "DELETE FROM golden_cases WHERE dataset_id = 2 AND id NOT IN (4)"
            )
            print(f"  {deleted}")

        after = await conn.fetch(
            "SELECT id, input_text, expected_behavior FROM golden_cases WHERE dataset_id = 2 ORDER BY id"
        )
        print(f"\nAfter: {len(after)} rows in dataset_id=2")
        for row in after:
            print(f"  id={row['id']}: {row['input_text']!r}")

        eval_count = await conn.fetchval("SELECT count(*) FROM evaluations WHERE golden_dataset_id = 2")
        version_count = await conn.fetchval("SELECT count(*) FROM prompt_versions WHERE prompt_id = 2")
        print(f"\nUnaffected, as expected: {eval_count} existing evaluation rows, "
              f"{version_count} existing prompt_version rows for prompt_id=2 (all untouched)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
