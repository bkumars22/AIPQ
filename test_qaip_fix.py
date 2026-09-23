import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"D:\KumarFolder\mydocs\LearningProject\ai-service")))
sys.path.insert(0, str(Path(r"D:\KumarFolder\mydocs\LearningProject")))

from aipq import AIPQClient, AIPQError, PromptQualityError
from prompt_library import PromptLibrary

QAIP_API_KEY = "aipq_FNbzKcAFBsgp8tTYjOjEdgJJNbsA4Y_LaLc76UaHNfc"
QAIP_PROJECT_ID = "2"


async def main():
    client = AIPQClient(api_key=QAIP_API_KEY, project_id=QAIP_PROJECT_ID, base_url="http://localhost:8001")
    try:
        result = await client.create_version(
            prompt_name="qaip_defect_explanation",
            content=PromptLibrary.QAIP_DEFECT_EXPLANATION.system,
            dataset="qaip_scope_golden",
            threshold=0.85,
            changed_by="kumar",
            change_message="Real prompt fix: handle out-of-scope requests within the required format (RULE 7) "
                            "instead of refusing outright — re-evaluate against the real MT-01 finding",
        )
        print(f"result: status={result.get('status')} quality_score={result.get('quality_score')}")
    except PromptQualityError as e:
        print(f"FAILED quality gate: score={e.score} threshold={e.threshold} details={e.details}")
    except AIPQError as e:
        print(f"AIPQError: {e}")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
