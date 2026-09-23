"""
Deletes the empty ARIA-v2 test-clutter project (id=3) created during
earlier debugging this session. Confirmed before running: zero prompts
and zero golden_datasets reference project_id=3 -- the only two tables
with a foreign key to projects -- so this is a clean, safe delete with
nothing else to cascade.
"""
import asyncio
import os

import asyncpg

DB_URL = os.environ["AIPQ_DATABASE_URL"]


async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        row = await conn.fetchrow("SELECT id, name, owner_email FROM projects WHERE id = 3")
        if row is None:
            print("Project id=3 not found -- already gone, nothing to do.")
            return
        print(f"Found: id={row['id']} name={row['name']!r} owner_email={row['owner_email']!r}")

        prompt_count = await conn.fetchval("SELECT count(*) FROM prompts WHERE project_id = 3")
        dataset_count = await conn.fetchval("SELECT count(*) FROM golden_datasets WHERE project_id = 3")
        print(f"Dependent rows: {prompt_count} prompts, {dataset_count} golden_datasets")
        if prompt_count or dataset_count:
            print("Not empty -- refusing to delete. Investigate first.")
            return

        await conn.execute("DELETE FROM projects WHERE id = 3")
        print("Deleted project id=3.")

        remaining = await conn.fetch("SELECT id, name FROM projects ORDER BY id")
        print("\nRemaining projects:")
        for r in remaining:
            print(f"  id={r['id']}: {r['name']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
