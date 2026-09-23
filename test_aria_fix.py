import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"D:\KumarFolder\mydocs\LearningProject\ai-service")))
sys.path.insert(0, str(Path(r"D:\KumarFolder\mydocs\LearningProject")))

from aipq import AIPQClient, AIPQError, PromptQualityError
from prompt_library import PromptLibrary

ARIA_API_KEY = "aipq_Y6mTpZl0viNfaFFJ0PF5jSIZlJ8pTfiC-1Ra7c3XC_A"
ARIA_PROJECT_ID = "1"


async def main():
    client = AIPQClient(api_key=ARIA_API_KEY, project_id=ARIA_PROJECT_ID, base_url="http://localhost:8001")
    try:
        result = await client.create_version(
            prompt_name="aria_socratic_system",
            content=PromptLibrary.ARIA_SOCRATIC.system,
            dataset="aria_adversarial_golden",
            threshold=0.90,
            changed_by="kumar",
            change_message="Re-evaluate against deduped + rubric-fixed adversarial golden cases (no prompt content change)",
        )
        print(f"result: status={result.get('status')} quality_score={result.get('quality_score')}")
    except PromptQualityError as e:
        print(f"FAILED quality gate: score={e.score} threshold={e.threshold} details={e.details}")
    except AIPQError as e:
        print(f"AIPQError: {e}")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
