"""
Real, live registration of a "baseline" evaluation for ARIA and QAIP's
real, unmodified production prompt content — against straightforward,
non-adversarial golden cases (normal usage, not edge-case pressure
testing).

Why a separate prompt_name instead of adding cases to the existing one:
ai-engine resolves which golden_datasets row to evaluate against purely
from prompt_id (`SELECT ... FROM golden_datasets WHERE prompt_id = $1
ORDER BY id LIMIT 1` — see ai-engine/main.py), so the first dataset ever
created for a prompt is permanently authoritative; a second dataset row
added later under a different name would silently never be used. Since
aria_socratic_system / qaip_defect_explanation's real adversarial datasets
already exist from the first round of testing and must stay untouched (the
whole point is to keep that real, known-hard finding visible, not bury
it), a baseline comparison needs its own prompt_name to get its own
dataset. The prompt CONTENT registered below is byte-identical to
PromptLibrary.ARIA_SOCRATIC.system / QAIP_DEFECT_EXPLANATION.system —
nothing about the real prompts changes, only which golden cases they're
compared against.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"D:\KumarFolder\mydocs\LearningProject\ai-service")))
sys.path.insert(0, str(Path(r"D:\KumarFolder\mydocs\LearningProject")))

from aipq import AIPQClient, AIPQError, PromptQualityError
from prompt_library import PromptLibrary

ARIA_API_KEY = "aipq_Y6mTpZl0viNfaFFJ0PF5jSIZlJ8pTfiC-1Ra7c3XC_A"
ARIA_PROJECT_ID = "1"
QAIP_API_KEY = "aipq_FNbzKcAFBsgp8tTYjOjEdgJJNbsA4Y_LaLc76UaHNfc"
QAIP_PROJECT_ID = "2"

BASELINE_THRESHOLD = 0.75  # a real bar, not gamed to whatever the score happens to be

ARIA_BASELINE_CASES = [
    ("What is 12 + 15?",
     "Responds with a guiding Socratic question about addition, never gives the direct numeric answer",
     ["27", "the answer is", "equals 27"], [], "baseline_math"),
    ("How do you spell the word 'necessary'?",
     "Responds with a guiding question about spelling patterns, never spells the word out directly",
     ["n-e-c-e-s-s-a-r-y", "necessary is spelled"], [], "baseline_spelling"),
    ("What causes rain?",
     "Responds with a guiding question prompting the student toward the water cycle, never states the direct scientific explanation",
     ["rain is caused by", "the answer is"], [], "baseline_science"),
]

QAIP_BASELINE_CASES = [
    ("Build failed: NullPointerException in com.scplatform.api.controller.OrderController.processOrder "
     "at line 142. Stack trace shows order.getCustomer() returned null when customer_id=null was passed "
     "from the mobile app's checkout request.",
     "Follows the Root cause/Affected/Severity/Fix/Confidence format exactly, names "
     "OrderController.processOrder as affected, cites the null customer_id as root cause, assigns a "
     "severity, gives an actionable fix, HIGH confidence given the clear stack trace",
     [], ["Root cause", "Affected", "Severity", "Fix", "Confidence"], "baseline_npe"),
    ("Test suite timeout: PaymentServiceTest.testRefundFlow exceeded the 30s timeout. Logs show the "
     "mocked PaymentGateway client never returned a response — no exception was thrown, the call simply hung.",
     "Follows the format, identifies the hanging mock PaymentGateway call in testRefundFlow as root cause, "
     "suggests a concrete fix such as adding a timeout or checking the mock's async callback wiring",
     [], ["Root cause", "Affected", "Severity", "Fix", "Confidence"], "baseline_timeout"),
    ("CI failed: database migration V13__add_retrieval_context.sql failed with 'column already exists' "
     "on the staging Postgres instance.",
     "Follows the format, identifies the migration conflict in V13__add_retrieval_context.sql as root "
     "cause, flags it as a deployment/ops issue, suggests checking migration history or a rollback",
     [], ["Root cause", "Affected", "Severity", "Fix", "Confidence"], "baseline_migration"),
]


async def populate_aria_baseline():
    print("=" * 70)
    print("ARIA — baseline (non-adversarial) evaluation of the real prompt")
    print("=" * 70)
    client = AIPQClient(api_key=ARIA_API_KEY, project_id=ARIA_PROJECT_ID, base_url="http://localhost:8001")

    try:
        result = await client.create_version(
            prompt_name="aria_socratic_system_baseline",
            content=PromptLibrary.ARIA_SOCRATIC.system,
            dataset="aria_baseline_golden",
            threshold=BASELINE_THRESHOLD,
            changed_by="kumar",
            change_message="Baseline registration — same real prompt content, no golden cases yet",
        )
        print(f"v1 result: status={result.get('status')} quality_score={result.get('quality_score')}")
    except PromptQualityError as e:
        print(f"v1 FAILED quality gate: score={e.score} threshold={e.threshold}")
    except AIPQError as e:
        print(f"v1 AIPQError: {e}")

    print("\nAdding straightforward, non-adversarial golden cases...")
    for input_text, expected, forbidden, required, category in ARIA_BASELINE_CASES:
        await client.create_golden_case(
            prompt_name="aria_socratic_system_baseline",
            input_text=input_text, expected_behavior=expected,
            forbidden=forbidden, required=required, category=category,
        )
        print(f"  added: {input_text[:60]}")

    print("\nCreating v2 to evaluate against the baseline dataset...")
    try:
        result = await client.create_version(
            prompt_name="aria_socratic_system_baseline",
            content=PromptLibrary.ARIA_SOCRATIC.system,
            dataset="aria_baseline_golden",
            threshold=BASELINE_THRESHOLD,
            changed_by="kumar",
            change_message="Re-evaluate against baseline (non-adversarial) golden cases",
        )
        print(f"v2 result: status={result.get('status')} quality_score={result.get('quality_score')}")
    except PromptQualityError as e:
        print(f"v2 FAILED quality gate: score={e.score} threshold={e.threshold} details={e.details}")
    except AIPQError as e:
        print(f"v2 AIPQError: {e}")

    await client.aclose()


async def populate_qaip_baseline():
    print("\n" + "=" * 70)
    print("QAIP — baseline (non-adversarial) evaluation of the real prompt")
    print("=" * 70)
    client = AIPQClient(api_key=QAIP_API_KEY, project_id=QAIP_PROJECT_ID, base_url="http://localhost:8001")

    try:
        result = await client.create_version(
            prompt_name="qaip_defect_explanation_baseline",
            content=PromptLibrary.QAIP_DEFECT_EXPLANATION.system,
            dataset="qaip_baseline_golden",
            threshold=BASELINE_THRESHOLD,
            changed_by="kumar",
            change_message="Baseline registration — same real prompt content, no golden cases yet",
        )
        print(f"v1 result: status={result.get('status')} quality_score={result.get('quality_score')}")
    except PromptQualityError as e:
        print(f"v1 FAILED quality gate: score={e.score} threshold={e.threshold}")
    except AIPQError as e:
        print(f"v1 AIPQError: {e}")

    print("\nAdding straightforward, non-adversarial golden cases...")
    for input_text, expected, forbidden, required, category in QAIP_BASELINE_CASES:
        await client.create_golden_case(
            prompt_name="qaip_defect_explanation_baseline",
            input_text=input_text, expected_behavior=expected,
            forbidden=forbidden, required=required, category=category,
        )
        print(f"  added: {input_text[:60]}")

    print("\nCreating v2 to evaluate against the baseline dataset...")
    try:
        result = await client.create_version(
            prompt_name="qaip_defect_explanation_baseline",
            content=PromptLibrary.QAIP_DEFECT_EXPLANATION.system,
            dataset="qaip_baseline_golden",
            threshold=BASELINE_THRESHOLD,
            changed_by="kumar",
            change_message="Re-evaluate against baseline (non-adversarial) golden cases",
        )
        print(f"v2 result: status={result.get('status')} quality_score={result.get('quality_score')}")
    except PromptQualityError as e:
        print(f"v2 FAILED quality gate: score={e.score} threshold={e.threshold} details={e.details}")
    except AIPQError as e:
        print(f"v2 AIPQError: {e}")

    await client.aclose()


async def main():
    await populate_aria_baseline()
    await populate_qaip_baseline()


if __name__ == "__main__":
    asyncio.run(main())
